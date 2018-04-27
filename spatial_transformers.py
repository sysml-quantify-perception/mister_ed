""" File that contains various parameterizations for spatial transformation
    styles. At its simplest, spatial transforms can be affine grids,
    parameterized  by 6 values. At their most complex, for a CxHxW type image
    grids can be parameterized by CxHxWx2 parameters.

    This file will define subclasses of nn.Module that will have parameters
    corresponding to the transformation parameters and will take in an image
    and output a transformed image.

    Further we'll also want a method to initialize each set to be the identity
    initially
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import utils.pytorch_utils as utils
from torch.autograd import Variable
import numpy as np


##############################################################################
#                                                                            #
#                               SKELETON CLASS                               #
#                                                                            #
##############################################################################

class ParameterizedTransformation(nn.Module):
    """ General class of transformations.
    All subclasses need the following methods:
    - norm: no args -> scalar variable
    - identity_params: shape -> TENSOR : takes an input shape and outputs
                       the subclass-specific parameter for the identity
                       transformation
    - forward : Variable -> Variable - is the transformation
    """

    def __init__(self):
        super(ParameterizedTransformation, self).__init__()

    def norm(self, lp='inf'):
        raise NotImplementedError("Need to call subclass's norm!")

    @classmethod
    def identity_params(self, shape):
        raise NotImplementedError("Need to call subclass's identity_params!")

    def merge_xform(self, other, self_mask):
        """ Takes in an other instance of this same class with the same
            shape of parameters (NxSHAPE) and a self_mask bytetensor of length
            N and outputs the merge between self's parameters for the indices
            of 1s in the self_mask and other's parameters for the indices of 0's
        ARGS:
            other: instance of same class as self with params of shape NxSHAPE -
                   the thing we merge with this one
            self_mask : ByteTensor (length N) - which indices of parameters we
                        keep from self, and which we keep from other
        RETURNS:
            New instance of this class that's merged between the self and other
            (same shaped params)
        """

        # JUST DO ASSERTS IN THE SKELETON CLASS
        assert self.__class__ == other.__class__

        self_params = self.xform_params.data
        other_params = other.xform_params.data
        assert self_params.shape == other_params.shape
        assert self_params.shape[0] == self_mask.shape[0]
        assert other_params.shape[0] == self_mask.shape[0]

        new_xform = self.__class__(shape=self.img_shape)

        new_params = utils.fold_mask(self.xform_params.data,
                                     other.xform_params.data, self_mask)
        new_xform.xform_params = nn.Parameter(new_params)

        return new_xform


    def forward(self, examples):
        raise NotImplementedError("Need to call subclass's forward!")





###############################################################################
#                                                                             #
#                  FULLY PARAMETERIZED SPATIAL TRANSFORMATION NETWORK         #
#                                                                             #
###############################################################################

class FullSpatial(ParameterizedTransformation):
    def __init__(self, *args, **kwargs):
        """ FullSpatial just has parameters that are the grid themselves.
            Forward then will just call grid sample using these params directly
        """

        super(FullSpatial, self).__init__()
        img_shape = kwargs['shape']
        self.img_shape = img_shape
        self.xform_params = nn.Parameter(self.identity_params(img_shape))


    @classmethod
    def identity_params(cls, shape):
        """ Returns some grid parameters such that the minibatch of images isn't
            changed when forward is called on it
        ARGS:
            shape: torch.Size - shape of the minibatch of images we'll be
                   transforming. First index should be num examples
        RETURNS:
            torch TENSOR (not variable!!!)
            if shape arg has shape NxCxHxW, this has shape NxCxHxWx2
        """

        # Work smarter not harder -- use idenity affine transforms here
        num_examples = shape[0]
        identity_affine_transform = torch.zeros(num_examples, 2, 3)

        identity_affine_transform[:,0,0] = 1
        identity_affine_transform[:,1,1] = 1

        return F.affine_grid(identity_affine_transform, shape).data

    def norm(self, lp='inf'):
        """ Returns the 'norm' of this transformation in terms of an LP norm on
            the parameters, summed across each transformation per minibatch
        ARGS:
            lp : int or 'inf' - which lp type norm we use
        """
        identity_params = Variable(self.identity_params(self.img_shape))
        return utils.summed_lp_norm(self.xform_params - identity_params, lp)


    def clip_params(self):
        """ Clips the parameters to be between -1 and 1 as required for
            grid_sample
        """
        clamp_params = torch.clamp(self.xform_params, -1, 1).data
        self.xform_params = nn.Parameter(clamp_params)


    def merge_xform(self, other, self_mask):
        """ Takes in an other instance of this same class with the same
            shape of parameters (NxSHAPE) and a self_mask bytetensor of length
            N and outputs the merge between self's parameters for the indices
            of 1s in the self_mask and other's parameters for the indices of 0's
        """
        super(FullSpatial, self).merge_xform(other, self_mask)

        new_xform = FullSpatial({'shape': self.img_shape})

        new_params = utils.fold_mask(self.xform_params.data,
                                     other.xform_params.data, self_mask)
        new_xform.xform_params = nn.Parameter(new_params)

        return new_xform



    def project_params(self, lp, lp_bound):
        """ Projects the params to be within lp_bound (according to an lp)
            of the identity map. First thing we do is clip the params to be
            valid, too
        ARGS:
            lp : int or 'inf' - which LP norm we use. Must be an int or the
                 string 'inf'
            lp_bound : float - how far we're allowed to go in LP land
        RETURNS:
            None, but modifies self.xform_params
        """

        assert isinstance(lp, int) or lp == 'inf'

        # clip first
        self.clip_params()

        # then project back

        if lp == 'inf':
            identity_params = self.identity_params(self.img_shape)
            clamp_params = utils.clamp_ref(self.xform_params.data,
                                               identity_params, lp_bound)
            self.xform_params = nn.Parameter(clamp_params)
        else:
            raise NotImplementedError("Only L-infinity bounds working for now ")


    def forward(self, x):
        # usual forward technique
        return F.grid_sample(x, self.xform_params)





###############################################################################
#                                                                             #
#                  AFFINE TRANSFORMATION NETWORK                              #
#                                                                             #
###############################################################################

class AffineTransform(ParameterizedTransformation):
    """ Affine transformation -- just has 6 parameters per example: 4 for 2d
        rotation, and 1 for translation in each direction
    """

    def __init__(self, *args, **kwargs):
        super(AffineTransform, self).__init__()
        img_shape = kwargs['shape']
        self.img_shape = img_shape
        self.xform_params = nn.Parameter(self.identity_params(img_shape))


    def norm(self, lp='inf'):
        identity_params = Variable(self.identity_params(self.img_shape))
        return utils.summed_lp_norm(self.xform_params - identity_params, lp)

    @classmethod
    def identity_params(cls, shape):
        """ Returns parameters for identity affine transformation
        ARGS:
            shape: torch.Size - shape of the minibatch of images we'll be
                   transforming. First index should be num examples
        RETURNS:
            torch TENSOR (not variable!!!)
            if shape arg has shape NxCxHxW, this has shape Nx2x3
        """

        # Work smarter not harder -- use idenity affine transforms here
        num_examples = shape[0]
        identity_affine_transform = torch.zeros(num_examples, 2, 3)

        identity_affine_transform[:,0,0] = 1
        identity_affine_transform[:,1,1] = 1

        return identity_affine_transform


    def project_params(self, lp, lp_bound):
        """ Projects the params to be within lp_bound (according to an lp)
            of the identity map. First thing we do is clip the params to be
            valid, too
        ARGS:
            lp : int or 'inf' - which LP norm we use. Must be an int or the
                 string 'inf'
            lp_bound : float - how far we're allowed to go in LP land
        RETURNS:
            None, but modifies self.xform_params
        """

        assert isinstance(lp, int) or lp == 'inf'

        diff = self.xform_params.data - self.identity_params(self.img_shape)
        new_diff = utils.batchwise_lp_project(diff, lp, lp_bound)
        self.xform_params.data.add_(new_diff - diff)


    def forward(self, x):
        # usual forward technique with affine grid
        grid = F.affine_grid(self.xform_params, x.shape)
        return F.grid_sample(x, grid)



class RotationTransform(AffineTransform):
    """ Rotations only -- only has one parameter, the angle by which we rotate
    """

    def __init__(self, *args, **kwargs):
        super(RotationTransform, self).__init__(shape=kwargs['shape'])
        '''
        img_shape = kwargs['shape']
        self.img_shape = img_shape
        self.xform_params = nn.Parameter(self.identity_params(img_shape))
        '''

    @classmethod
    def identity_params(cls, shape):
        num_examples = shape[0]
        return torch.zeros(num_examples)

    def make_grid(self, x):
        assert isinstance(x, Variable)
        cos_xform = self.xform_params.cos()
        sin_xform = self.xform_params.sin()
        zeros = Variable(torch.zeros(self.xform_params.shape))

        affine_xform = torch.stack([cos_xform, -sin_xform, zeros,
                                    sin_xform, cos_xform,  zeros])
        affine_xform = affine_xform.transpose(0, 1).contiguous().view(-1, 2, 3)

        return F.affine_grid(affine_xform, x.shape)

    def forward(self, x):
        return F.grid_sample(x, self.make_grid(x))



class TranslationTransform(AffineTransform):
    """ Rotations only -- only has one parameter, the angle by which we rotate
    """

    def __init__(self, *args, **kwargs):
        super(TranslationTransform, self).__init__(shape=kwargs['shape'])


    @classmethod
    def identity_params(cls, shape):
        num_examples = shape[0]
        return torch.zeros(num_examples, 2) # x and y translation only

    def make_grid(self, x):
        assert isinstance(x, Variable)

        ones = Variable(torch.ones(self.xform_params.shape[0]))
        zeros = Variable(torch.zeros(self.xform_params.shape[0]))

        affine_xform = torch.stack([ones, zeros, self.xform_params[:,0],
                                    zeros, ones, self.xform_params[:,1]])

        affine_xform = affine_xform.transpose(0, 1).contiguous().view(-1, 2, 3)

        return F.affine_grid(affine_xform, x.shape)

    def forward(self, x):
        return F.grid_sample(x, self.make_grid(x))






