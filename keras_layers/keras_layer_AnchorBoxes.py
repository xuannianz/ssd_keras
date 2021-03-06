"""
A custom Keras layer to generate anchor boxes.

Copyright (C) 2018 Pierluigi Ferrari

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import division
import numpy as np
import keras.backend as K
from keras.engine.topology import InputSpec
from keras.engine.topology import Layer

from bounding_box_utils.bounding_box_utils import convert_coordinates


class AnchorBoxes(Layer):
    """
    A Keras layer to create an output tensor containing anchor box coordinates and variances based on the input tensor
    and the passed arguments.

    A set of 2D anchor boxes of different aspect ratios is created for each spatial unit of the input tensor. The number
    of anchor boxes created per unit depends on the arguments `aspect_ratios` and `two_boxes_for_ar1`, in the default
    case it is 4. The boxes are parameterized by the coordinate tuple `(xmin, ymin, xmax, ymax)`.

    The logic implemented by this layer is identical to the logic of function `generate_anchor_boxes_for_layer`
    in the module `ssd_input_encoder.py`.

    The purpose of having this layer in the network is to make the model self-sufficient at inference time.
    Since the model is predicting offsets to the anchor boxes (rather than predicting absolute box coordinates directly)
    , one needs to know the anchor box coordinates in order to construct the final prediction boxes from the predicted
    offsets.
    If the model's output tensor did not contain the anchor box coordinates, the necessary information to convert the
    predicted offsets back to absolute coordinates would be missing in the model output. The reason why it is necessary
    to predict offsets to the anchor boxes rather than to predict absolute box coordinates directly is explained in
    `README.md`.

    Input shape:
        4D tensor of shape `(batch, channels, height, width)` if `dim_ordering = 'th'`
        or `(batch, height, width, channels)` if `dim_ordering = 'tf'`.

    Output shape:
        5D tensor of shape `(batch, height, width, n_boxes, 8)`.
        The last axis contains the four anchor box coordinates and the four variance values for each box.
    """

    def __init__(self,
                 img_height,
                 img_width,
                 this_scale,
                 next_scale,
                 aspect_ratios=(0.5, 1.0, 2.0),
                 two_boxes_for_ar1=True,
                 this_steps=None,
                 this_offsets=None,
                 clip_boxes=False,
                 variances=(0.1, 0.1, 0.2, 0.2),
                 coords='centroids',
                 normalize_coords=False,
                 **kwargs):
        """
        All arguments need to be set to the same values as in the box encoding process, otherwise the behavior is
        undefined.
        Some of these arguments are explained in more detail in the documentation of the `SSDBoxEncoder` class.

        Arguments:
            img_height (int): The height of the input images.
            img_width (int): The width of the input images.
            this_scale (float): A float in (0, 1], the scaling factor for the size of the generated anchor boxes
                as a fraction of the shorter side of the input image.
            next_scale (float): A float in (0, 1], the next larger scaling factor. Only relevant if
                `self.two_boxes_for_ar1 == True`.
            aspect_ratios (tuple/list, optional): The tuple/list of aspect ratios for which default boxes are to be
                generated for this layer.
            two_boxes_for_ar1 (bool, optional): Only relevant if `aspect_ratios` contains 1.
                If `True`, two default boxes will be generated for aspect ratio 1. The first will be generated
                using the scaling factor for the respective layer, the second one will be generated using
                geometric mean of said scaling factor and next bigger scaling factor.
            clip_boxes (bool, optional): If `True`, clips the anchor box coordinates to stay within image boundaries.
            variances (tuple/list, optional): A list of 4 floats >0. The anchor box offset for each coordinate will be
                divided by its respective variance value.
            coords (str, optional): The box coordinate format to be used internally in the model (i.e. this is not the
                input format of the ground truth labels).
                Can be either 'centroids' for the format `(cx, cy, w, h)` (box center coordinates, width, and height),
                'corners' for the format `(xmin, ymin, xmax,  ymax)`,
                or 'minmax' for the format `(xmin, xmax, ymin, ymax)`.
            normalize_coords (bool, optional): Set to `True` if the model uses relative instead of absolute coordinates,
                i.e. if the model predicts box coordinates within [0,1] instead of absolute coordinates.
        """

        ############################################################################
        # Get a few exceptions out of the way.
        ############################################################################
        if K.backend() != 'tensorflow':
            raise TypeError(
                "This layer only supports TensorFlow at the moment, "
                "but you are using the {} backend.".format(K.backend()))

        if not (isinstance(img_height, int) and isinstance(img_width, int)):
            raise ValueError('`img_height` and `img_width` must be float')
        elif not (img_height > 0 and img_width > 0):
            raise ValueError('`img_height` and `img_width` must be greater than 0')
        else:
            self.img_height = img_height
            self.img_width = img_width

        if not (isinstance(this_scale, float) and isinstance(next_scale, float)):
            raise ValueError('`this_scale` and `next_scale` must be float')
        elif not ((0 < this_scale) and (0 < next_scale)):
            raise ValueError(
                "`this_scale` and `next_scale` must be > 0"
                "but `this_scale` == {}, `next_scale` == {}".format(this_scale, next_scale))
        else:
            self.this_scale = this_scale
            self.next_scale = next_scale

        if not (isinstance(aspect_ratios, (list, tuple)) and aspect_ratios):
            raise ValueError("Aspect ratios must be a list or tuple and not empty")
        # NOTE 当 aspect_ratios 为 () 或 [], np.any(np.array(aspect_ratios)) <=0 为 False, 所以必须有上面的判断
        elif np.any(np.array(aspect_ratios) <= 0):
            raise ValueError("All aspect ratios must be greater than zero.")
        else:
            self.aspect_ratios = aspect_ratios

        if not (isinstance(variances, (list, tuple)) and len(variances) == 4):
            # We need one variance value for each of the four box coordinates
            raise ValueError("4 variance values must be passed, but {} values were received.".format(len(variances)))
        else:
            variances = np.array(variances)
            if np.any(variances <= 0):
                raise ValueError("All variances must be >0, but the variances given are {}".format(variances))
            else:
                self.variances = variances

        if coords not in ('minmax', 'centroids', 'corners'):
            raise ValueError("Unexpected value for `coords`. Supported values are 'minmax', 'corners' and 'centroids'.")
        else:
            self.coords = coords

        if this_steps is not None:
            if not ((isinstance(this_steps, (list, tuple)) and (len(this_steps) == 2)) or
                    isinstance(this_steps, (int, float))):
                raise ValueError("This steps must be a 2-int/float list/tuple or a int/float")
            else:
                self.this_steps = this_steps
        else:
            self.this_steps = this_steps

        if this_offsets is not None:
            if not ((isinstance(this_offsets, (list, tuple)) and (len(this_offsets) == 2)) or
                    isinstance(this_offsets, (int, float))):
                raise ValueError("This steps must be a 2-int/float list/tuple or a int/float")
            else:
                self.this_offsets = this_offsets
        else:
            self.this_offsets = this_offsets

        if not (isinstance(two_boxes_for_ar1, bool)):
            raise ValueError('`two_boxes_for_ar1` must be bool')
        else:
            self.two_boxes_for_ar1 = two_boxes_for_ar1

        if not (isinstance(clip_boxes, bool)):
            raise ValueError('`clip_boxes` must be bool')
        else:
            self.clip_boxes = clip_boxes

        if not (isinstance(normalize_coords, bool)):
            raise ValueError('`normalize_coords` must be bool')
        else:
            self.normalize_coords = normalize_coords

        # Compute the number of boxes per cell
        if (1 in aspect_ratios) and two_boxes_for_ar1:
            self.n_boxes = len(aspect_ratios) + 1
        else:
            self.n_boxes = len(aspect_ratios)
        super(AnchorBoxes, self).__init__(**kwargs)

    def build(self, input_shape):
        # UNCLEAR
        self.input_spec = [InputSpec(shape=input_shape)]
        super(AnchorBoxes, self).build(input_shape)

    def call(self, x, mask=None):
        """
        Return an anchor box tensor based on the shape of the input tensor.
        The logic implemented here is identical to the logic of function `generate_anchor_boxes_for_layer` in the module
        `ssd_box_encode_decode_utils.py`.
        Note that this tensor does not participate in any graph computations at runtime.
        It is being created as a constant once during graph creation and is just being output along with the rest of the
        model output during runtime.
        Because of this, all logic is implemented as Numpy array operations and it is sufficient to convert the
        resulting Numpy array into a Keras tensor at the very end before outputting it.

        Arguments:
            x (tensor): 4D tensor of shape
                `(batch, channels, height, width)` if `dim_ordering = 'th'`
                or `(batch, height, width, channels)` if `dim_ordering = 'tf'`.
                The input for this layer must be the output of the localization predictor layer.
            # UNCLEAR mask 是啥?
            mask:
        """

        # Compute box width and height for each aspect ratio
        # The shorter side of the image will be used to compute `w` and `h` using `scale` and `aspect_ratios`.
        size = min(self.img_height, self.img_width)
        # Compute the box widths and and heights for all aspect ratios
        wh_list = []
        for aspect_ratio in self.aspect_ratios:
            if aspect_ratio == 1:
                # Compute the regular anchor box for aspect ratio 1.
                box_height = box_width = self.this_scale * size
                wh_list.append((box_width, box_height))
                if self.two_boxes_for_ar1:
                    # Compute one slightly larger version using the geometric mean of this scale value and the next.
                    # NOTE 几何平均数, 就是当 aspect_ratios 为 1 时取两个 boxes
                    box_height = box_width = np.sqrt(self.this_scale * self.next_scale) * size
                    wh_list.append((box_width, box_height))
            else:
                box_height = self.this_scale * size / np.sqrt(aspect_ratio)
                box_width = self.this_scale * size * np.sqrt(aspect_ratio)
                wh_list.append((box_width, box_height))
        # shape 为 (n_boxes, 2)
        wh_list = np.array(wh_list)

        # We need the shape of the input tensor
        if K.image_dim_ordering() == 'tf':
            # FIXME
            batch_size, feature_map_height, feature_map_width, feature_map_channels = K.int_shape(x)
            # batch_size, feature_map_height, feature_map_width, feature_map_channels = x._keras_shape
        else:
            # Not yet relevant since TensorFlow is the only supported backend right now,
            # but it can't harm to have this in here for the future
            batch_size, feature_map_height, feature_map_width, feature_map_channels = K.int_shape(x)
            # batch_size, feature_map_channels, feature_map_height, feature_map_width = x._keras_shape

        ##################################################################################
        # Compute the grid of box center points. They are identical for all aspect ratios.
        ##################################################################################

        # 1. Compute the step sizes,
        # i.e. how far apart the anchor box center points will be vertically and horizontally.
        if self.this_steps is None:
            # 假设 box4, img_height,img_width=512, 那么 feature_map_height,feature_map_width=512 / 2 ^ 3 = 64
            # 那么 step_height,step_width = 512 / 64 = 8
            # 意思是 feature_map 是 64*64 的方格, 一个方格表示原图的 8*8 个像素, 每一个 step 移动一个方格
            step_height = self.img_height / feature_map_height
            step_width = self.img_width / feature_map_width
        else:
            if isinstance(self.this_steps, (list, tuple)):
                step_height = self.this_steps[0]
                step_width = self.this_steps[1]
            # 相当于 elif isinstance(self.this_steps, (int, float)):
            else:
                step_height = self.this_steps
                step_width = self.this_steps

        # 2. Compute the offsets, i.e.
        # at what pixel values the first anchor box center point will be from the top and from the left of the image.
        if self.this_offsets is None:
            offset_height = 0.5
            offset_width = 0.5
        else:
            if isinstance(self.this_offsets, (list, tuple)):
                offset_height = self.this_offsets[0]
                offset_width = self.this_offsets[1]
            # 相当于 elif isinstance(self.this_offsets, (int, float)):
            else:
                offset_height = self.this_offsets
                offset_width = self.this_offsets

        # 3. Now that we have the offsets and step sizes, compute the grid of anchor box center points.
        # np.linspace 参见 https://docs.scipy.org/doc/numpy/reference/generated/numpy.linspace.html
        # 第一个参数 start 表示区间开始, 第二个参数 stop 表示区间结尾, 第三个参数 num, 表示个数，默认包含 stop
        # 如 box4, np.linspace(0.5 * 8, 63.5 * 8, 64), cy=np.array([4, 12,..., 500, 508])
        cy = np.linspace(offset_height * step_height, (offset_height + feature_map_height - 1) * step_height,
                         feature_map_height)
        # 如 box4, np.linspace(0.5 * 8, 63.5 * 8, 64), cx=np.array([4, 12,..., 500, 508])
        cx = np.linspace(offset_width * step_width, (offset_width + feature_map_width - 1) * step_width,
                         feature_map_width)
        # 如 box4, cx_grid=np.array([[4,12,...508],[4,12,...508],..., [4,12,...508]), shape 为 (64, 64)
        # cy_grid=np.array([[4,4,...4],[12,12,...12],...,[508,508,...508]]), shape 为 (64, 64)
        cx_grid, cy_grid = np.meshgrid(cx, cy)
        # This is necessary for np.tile() to do what we want further down
        # 如 box4, shape 变为 (64, 64, 1)
        cx_grid = np.expand_dims(cx_grid, -1)
        cy_grid = np.expand_dims(cy_grid, -1)

        # Create a 4D tensor template of shape `(feature_map_height, feature_map_width, n_boxes, 4)`
        # where the last dimension will contain `(cx, cy, w, h)`
        boxes_tensor = np.zeros((feature_map_height, feature_map_width, self.n_boxes, 4))
        # np.tile() 返回的数组的 shape 为 (feature_map_height, feature_map_width, n_boxes)
        # Set cx
        boxes_tensor[:, :, :, 0] = np.tile(cx_grid, (1, 1, self.n_boxes))
        # Set cy
        boxes_tensor[:, :, :, 1] = np.tile(cy_grid, (1, 1, self.n_boxes))
        # Set w
        boxes_tensor[:, :, :, 2] = wh_list[:, 0]
        # Set h
        boxes_tensor[:, :, :, 3] = wh_list[:, 1]

        # Convert `(cx, cy, w, h)` to `(xmin, ymin, xmax, ymax)`
        # 转换是为了做 clip
        boxes_tensor = convert_coordinates(boxes_tensor, start_index=0, conversion='centroids2corners')

        # If `clip_boxes` is enabled, clip the coordinates to lie within the image boundaries
        if self.clip_boxes:
            x_coords = boxes_tensor[:, :, :, [0, 2]]
            x_coords[x_coords >= self.img_width] = self.img_width - 1
            x_coords[x_coords < 0] = 0
            # 记得 tf 是不能做这样的操作的
            boxes_tensor[:, :, :, [0, 2]] = x_coords
            y_coords = boxes_tensor[:, :, :, [1, 3]]
            y_coords[y_coords >= self.img_height] = self.img_height - 1
            y_coords[y_coords < 0] = 0
            boxes_tensor[:, :, :, [1, 3]] = y_coords

        # If `normalize_coords` is enabled, normalize the coordinates to be within [0,1]
        if self.normalize_coords:
            boxes_tensor[:, :, :, [0, 2]] /= self.img_width
            boxes_tensor[:, :, :, [1, 3]] /= self.img_height

        # TODO: Implement box limiting directly for `(cx, cy, w, h)`
        #  so that we don't have to unnecessarily convert back and forth.
        if self.coords == 'centroids':
            # Convert `(xmin, ymin, xmax, ymax)` back to `(cx, cy, w, h)`.
            boxes_tensor = convert_coordinates(boxes_tensor, start_index=0, conversion='corners2centroids',
                                               border_pixels='half')
        elif self.coords == 'minmax':
            # Convert `(xmin, ymin, xmax, ymax)` to `(xmin, xmax, ymin, ymax).
            boxes_tensor = convert_coordinates(boxes_tensor, start_index=0, conversion='corners2minmax',
                                               border_pixels='half')

        # Create a tensor to contain the variances and append it to `boxes_tensor`.
        # This tensor has the same shape as `boxes_tensor`
        # and simply contains the same 4 variance values for every position in the last axis.
        # Has shape `(feature_map_height, feature_map_width, n_boxes, 4)`
        variances_tensor = np.zeros_like(boxes_tensor)
        # Long live broadcasting
        variances_tensor += self.variances
        # Now `boxes_tensor` becomes a tensor of shape `(feature_map_height, feature_map_width, n_boxes, 8)`
        boxes_tensor = np.concatenate((boxes_tensor, variances_tensor), axis=-1)
        # Now prepend one dimension to `boxes_tensor` to account for the batch size and tile it along
        # 沿着 batch_size 那一维进行 tile
        # The result will be a 5D tensor of shape `(batch_size, feature_map_height, feature_map_width, n_boxes, 8)`
        boxes_tensor = np.expand_dims(boxes_tensor, axis=0)
        boxes_tensor = K.tile(K.constant(boxes_tensor, dtype='float32'), (K.shape(x)[0], 1, 1, 1, 1))

        return boxes_tensor

    def compute_output_shape(self, input_shape):
        if K.image_dim_ordering() == 'tf':
            batch_size, feature_map_height, feature_map_width, feature_map_channels = input_shape
        else:
            # Not yet relevant since TensorFlow is the only supported backend right now,
            # but it can't harm to have this in here for the future
            batch_size, feature_map_channels, feature_map_height, feature_map_width = input_shape
        return batch_size, feature_map_height, feature_map_width, self.n_boxes, 8

    def get_config(self):
        # UNCLEAR: get_config 有什么用?
        config = {
            'img_height': self.img_height,
            'img_width': self.img_width,
            'this_scale': self.this_scale,
            'next_scale': self.next_scale,
            'aspect_ratios': list(self.aspect_ratios),
            'two_boxes_for_ar1': self.two_boxes_for_ar1,
            'clip_boxes': self.clip_boxes,
            'variances': list(self.variances),
            'coords': self.coords,
            'normalize_coords': self.normalize_coords
        }
        base_config = super(AnchorBoxes, self).get_config()
        # update inplace
        base_config.update(config)
        # FIXME
        return base_config
        # return dict(list(base_config.items()) + list(config.items()))
