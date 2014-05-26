# encoding: utf-8
# ---------------------------------------------------------------------------
#  Copyright (C) 2008-2014, IPython Development Team and Enthought, Inc.
#  Distributed under the terms of the BSD License.  See COPYING.rst.
# ---------------------------------------------------------------------------
"""
The Distarray data structure.`DistArray` objects are proxies for collections of
`LocalArray` objects. They are meant to roughly emulate NumPy `ndarrays`.
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from __future__ import absolute_import

import operator
from itertools import product
from functools import reduce
from collections import Sequence

import numpy as np

import distarray
from distarray.metadata_utils import sanitize_indices
from distarray.dist.maps import Distribution
from distarray.utils import _raise_nie

__all__ = ['DistArray']


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------

class DistArray(object):

    __array_priority__ = 20.0

    def __init__(self, distribution, dtype=float):
        """Creates an empty DistArray according to the `distribution` given."""
        # FIXME: code duplication with context.py.
        ctx = distribution.context
        # FIXME: this is bad...
        comm_name = ctx.comm
        # FIXME: and this is bad...
        da_key = ctx._generate_key()
        ddpr = distribution.get_dim_data_per_rank()
        ddpr_name, dtype_name = ctx._key_and_push(ddpr, dtype)
        cmd = ('{da_key} = distarray.local.empty('
               'distarray.local.maps.Distribution('
               '{ddpr_name}[{comm_name}.Get_rank()], '
               '{comm_name}), {dtype_name})')
        ctx._execute(cmd.format(**locals()), targets=distribution.targets)
        self.distribution = distribution
        self.key = da_key
        self._dtype = dtype

    @classmethod
    def from_localarrays(cls, key, context=None, targets=None, distribution=None,
                         dtype=None):
        """The caller has already created the LocalArray objects.  `key` is
        their name on the engines.  This classmethod creates a DistArray that
        refers to these LocalArrays.

        Either a `context` or a `distribution` must also be provided.  If
        `context` is provided, a ``dim_data_per_rank`` will be pulled from
        the existing ``LocalArray``\s and a ``Distribution`` will be created
        from it.   If `distribution` is provided, it should accurately
        reflect the  distribution of the existing ``LocalArray``\s.

        If `dtype` is not provided, it will be fetched from the engines.
        """

        def get_dim_datas_and_dtype(arr):
            return (arr.dim_data, arr.dtype)

        da = cls.__new__(cls)
        da.key = key

        if (context is None) == (distribution is None):
            errmsg = "Must provide `context` or `distribution` but not both."
            raise RuntimeError(errmsg)

        # has context, get dist and dtype
        elif (distribution is None) and (dtype is None):
            res = context.apply(get_dim_datas_and_dtype, args=(key,),
                                targets=targets)
            dim_datas = [i[0] for i in res]
            dtypes = [i[1] for i in res]
            da._dtype = dtypes[0]
            da.distribution = Distribution.from_dim_data_per_rank(context,
                                                                  dim_datas,
                                                                  targets)

        # has context and dtype, get dist
        elif (distribution is None) and (dtype is not None):
            da._dtype = dtype
            dim_datas = context.apply(getattr, args=(key, 'dim_data'),
                                      targets=targets)
            da.distribution = Distribution.from_dim_data_per_rank(context,
                                                                  dim_datas,
                                                                  targets)

        # has distribution, get dtype
        elif (distribution is not None) and (dtype is None):
            da.distribution = distribution
            da._dtype = distribution.context.apply(getattr,
                                                   args=(key, 'dtype'),
                                                   targets=[0])[0]
        # has distribution and dtype
        elif (distribution is not None) and (dtype is not None):
            da.distribution = distribution
            da._dtype = dtype

        # sanity check that I didn't miss any cases above, because this is a
        # confusing function
        else:
            assert(False)
        return da

    def __del__(self):
        self.context.delete_key(self.key, self.targets)

    def __repr__(self):
        s = '<DistArray(shape=%r, targets=%r)>' % \
            (self.shape, self.targets)
        return s

    def _process_return_value(self, result, return_proxy, index, targets):

        if return_proxy:
            # proxy returned as result of slice
            # slicing shouldn't alter the dtype
            return DistArray.from_localarrays(key=result,
                                              context=self.context,
                                              targets=targets,
                                              dtype=self.dtype)

        elif isinstance(result, Sequence):
            somethings = [i for i in result if i is not None]
            if len(somethings) == 0:
                # using checked_getitem and all return None
                raise IndexError("Index %r is is not present." % (index,))
            if len(somethings) == 1:
                return somethings[0]
            else:
                return result
        else:
            assert False  # impossible is nothing


    def __getitem__(self, index):

        # to be run locally
        def checked_getitem(arr, index):
            return arr.global_index.checked_getitem(index)

        # to be run locally
        def raw_getitem(arr, index):
            return arr.global_index[index]

        # to be run locally
        def get_slice(arr, index, ddpr, comm):
            from distarray.local.maps import Distribution
            local_distribution = Distribution(ddpr[comm.Get_rank()],
                                              comm=comm)
            return arr.global_index.get_slice(index, local_distribution)

        return_type, index = sanitize_indices(index, ndim=self.ndim,
                                              shape=self.shape)
        return_proxy = (return_type == 'view')
        targets = self.distribution.owning_targets(index)

        args = [self.key, index]
        if self.distribution.has_precise_index:
            if return_proxy:  # returning a new DistArray view
                new_distribution = self.distribution.slice(index)
                ddpr = new_distribution.get_dim_data_per_rank()
                args.extend([ddpr, new_distribution.comm])
                local_fn = get_slice
            else:  # returning a value
                local_fn = raw_getitem
        else:  # returning a value from unstructured
            local_fn = checked_getitem

        result = self.context.apply(local_fn, args=args,
                                    targets=targets,
                                    return_proxy=return_proxy)
        return self._process_return_value(result, return_proxy, index, targets)


    def __setitem__(self, index, value):
        #TODO: FIXME: major performance improvements possible here.
        # Especially when `index == slice(None)` and value is an
        # ndarray, since for block and cyclic, we can generate slices of
        # `value` and assign to local arrays. This would dramatically
        # improve the fromndarray method's performance.

        # to be run locally
        def checked_setitem(arr, index, value):
            return arr.global_index.checked_setitem(index, value)

        # to be run locally
        def raw_setitem(arr, index, value):
            arr.global_index[index] = value

        _, index = sanitize_indices(index, ndim=self.ndim, shape=self.shape)

        targets = self.distribution.owning_targets(index)
        args = (self.key, index, value)
        if self.distribution.has_precise_index:
            self.context.apply(raw_setitem, args=args, targets=targets)
        else:
            result = self.context.apply(checked_setitem, args=args,
                                        targets=targets)
            result = [i for i in result if i is not None]
            if len(result) > 1:
                raise IndexError("Setting more than one result (%s) is "
                                 "not supported yet." % (result,))
            elif result == []:
                raise IndexError("Index %s is out of bounds" % (index,))

    @property
    def context(self):
        return self.distribution.context

    @property
    def shape(self):
        return self.distribution.shape

    @property
    def global_size(self):
        return reduce(operator.mul, self.shape)

    @property
    def dist(self):
        return self.distribution.dist

    @property
    def grid_shape(self):
        return self.distribution.grid_shape

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def nbytes(self):
        return self.global_size * self.itemsize

    @property
    def dtype(self):
        return self._dtype

    @property
    def itemsize(self):
        return self._dtype.itemsize

    @property
    def targets(self):
        return self.distribution.targets

    def tondarray(self):
        """Returns the distributed array as an ndarray."""
        arr = np.empty(self.shape, dtype=self.dtype)
        local_name = self.context._generate_key()
        self.context._execute('%s = %s.copy()' % (local_name, self.key), targets=self.targets)
        local_arrays = self.context._pull(local_name, targets=self.targets)
        for local_array in local_arrays:
            maps = (list(ax_map.global_iter) for ax_map in
                    local_array.distribution)
            for index in product(*maps):
                arr[index] = local_array.global_index[index]
        return arr

    toarray = tondarray

    def get_dist_matrix(self):
        key = self.context._generate_key()
        self.context._execute0(
            '%s = %s.get_dist_matrix()' % (key, self.key),
            targets=self.targets)
        result = self.context._pull(key, targets=self.targets[0])
        return result

    def fill(self, value):
        def inner_fill(arr, value):
            arr.fill(value)
        self.context.apply(inner_fill, args=(self.key, value), targets=self.targets)

    # TODO FIXME: implement axis and out kwargs.
    def sum(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.sum(%s,%s)' % subs,
                              targets=self.targets)
        result = self.context._pull(result_key, targets=self.targets[0])
        return result

    def mean(self, axis=None, dtype=float, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.mean(axis=%s, dtype=%s)' % subs,
                              targets=self.targets)
        result = self.context._pull(result_key, targets=self.targets[0])
        return result

    def var(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.var(%s,%s)' % subs,
                              targets=self.targets)
        result = self.context._pull(result_key, targets=self.targets[0])
        return result

    def std(self, axis=None, dtype=None, out=None):
        if axis or out is not None:
            _raise_nie()
        keys = self.context._key_and_push(axis, dtype)
        result_key = self.context._generate_key()
        subs = (result_key, self.key) + keys
        self.context._execute('%s = %s.std(%s,%s)' % subs,
                              targets=self.targets)
        result = self.context._pull(result_key, targets=self.targets[0])
        return result

    def get_ndarrays(self):
        """Pull the local ndarrays from the engines.

        Returns
        -------
        list of ndarrays
            one ndarray per process

        """
        key = self.context._generate_key()
        self.context._execute('%s = %s.get_localarray()' % (key, self.key),
                              targets=self.targets)
        result = self.context._pull(key, targets=self.targets)
        return result

    def get_localarrays(self):
        """Pull the LocalArray objects from the engines.

        Returns
        -------
        list of localarrays
            one localarray per process

        """
        result = self.context._pull(self.key, targets=self.targets)
        return result

    def get_localshapes(self):
        key = self.context._generate_key()
        self.context._execute('%s = %s.local_shape' % (key, self.key),
                              targets=self.targets)
        result = self.context._pull(key, targets=self.targets)
        return result

    # Binary operators

    def _binary_op_from_ufunc(self, other, func, rop_str=None, *args, **kwargs):
        if hasattr(other, '__array_priority__') and hasattr(other, rop_str):
            if other.__array_priority__ > self.__array_priority__:
                rop = getattr(other, rop_str)
                return rop(self)
        return func(self, other, *args, **kwargs)

    def _rbinary_op_from_ufunc(self, other, func, lop_str, *args, **kwargs):
        if hasattr(other, '__array_priority__') and hasattr(other, lop_str):
            if other.__array_priority__ > self.__array_priority__:
                lop = getattr(other, lop_str)
                return lop(self)
        return func(other, self, *args, **kwargs)

    def __add__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.add, '__radd__', *args, **kwargs)

    def __sub__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.subtract, '__rsub__', *args, **kwargs)

    def __mul__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.multiply, '__rmul__', *args, **kwargs)

    def __div__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.divide, '__rdiv__', *args, **kwargs)

    def __truediv__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.true_divide, '__rtruediv__', *args, **kwargs)

    def __floordiv__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.floor_divide, '__rfloordiv__', *args, **kwargs)

    def __mod__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.mod, '__rdiv__', *args, **kwargs)

    def __pow__(self, other, modulo=None, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.power, '__rpower__', *args, **kwargs)

    def __lshift__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.left_shift, '__rlshift__', *args, **kwargs)

    def __rshift__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.right_shift, '__rrshift__', *args, **kwargs)

    def __and__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.bitwise_and, '__rand__', *args, **kwargs)

    def __or__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.bitwise_or, '__ror__', *args, **kwargs)

    def __xor__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.bitwise_xor, '__rxor__', *args, **kwargs)

    # Binary - right versions

    def __radd__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.add, '__add__', *args, **kwargs)

    def __rsub__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.subtract, '__sub__', *args, **kwargs)

    def __rmul__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.multiply, '__mul__', *args, **kwargs)

    def __rdiv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.divide, '__div__', *args, **kwargs)

    def __rtruediv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.true_divide, '__truediv__', *args, **kwargs)

    def __rfloordiv__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.floor_divide, '__floordiv__', *args, **kwargs)

    def __rmod__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.mod, '__mod__', *args, **kwargs)

    def __rpow__(self, other, modulo=None, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.power, '__pow__', *args, **kwargs)

    def __rlshift__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.left_shift, '__lshift__', *args, **kwargs)

    def __rrshift__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.right_shift, '__rshift__', *args, **kwargs)

    def __rand__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.bitwise_and, '__and__', *args, **kwargs)

    def __ror__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.bitwise_or, '__or__', *args, **kwargs)

    def __rxor__(self, other, *args, **kwargs):
        return self._rbinary_op_from_ufunc(other, distarray.dist.bitwise_xor, '__xor__', *args, **kwargs)

    def __neg__(self, *args, **kwargs):
        return distarray.dist.negative(self, *args, **kwargs)

    def __pos__(self, *args, **kwargs):
        return self

    def __abs__(self, *args, **kwargs):
        return distarray.dist.abs(self, *args, **kwargs)

    def __invert__(self, *args, **kwargs):
        return distarray.dist.invert(self, *args, **kwargs)

    # Boolean comparisons

    def __lt__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.less, '__lt__', *args, **kwargs)

    def __le__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.less_equal, '__le__', *args, **kwargs)

    def __eq__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.equal, '__eq__', *args, **kwargs)

    def __ne__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.not_equal, '__ne__', *args, **kwargs)

    def __gt__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.greater, '__gt__', *args, **kwargs)

    def __ge__(self, other, *args, **kwargs):
        return self._binary_op_from_ufunc(other, distarray.dist.greater_equal, '__ge__', *args, **kwargs)
