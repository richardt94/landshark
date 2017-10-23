"""Importing routines for tif data."""

import os.path
import logging

import numpy as np
import tables
from typing import List, Union, Callable, Iterator, Tuple

from landshark.importers.tifread import ImageStack

log = logging.getLogger(__name__)

MissingValueList = List[Union[np.float32, np.int32, None]]


class _Categories:
    """Class that gets the number of categories for features."""
    def __init__(self, missing_values, max_categories=5000) -> None:
        n_features = len(missing_values)
        self._values = [set() for _ in range(n_features)]
        self._maps = [dict() for _ in range(n_features)]
        for i, k in enumerate(missing_values):
            if k is not None:
                self._values[i].add(k)
                self._maps[i][k] = 0
        self._max_categories = max_categories


    def update(self, array: np.ndarray):
        new_array = np.copy(array)
        for i, data in enumerate(array.T):
            unique_vals = np.unique(data)
            new_values = set(unique_vals).difference(self._values[i])
            nstart = len(self._values[i])
            nstop = nstart + len(new_values) + 1
            new_indices = range(nstart, nstop)
            self._maps[i].update(zip(new_values, new_indices))
            self._values[i].update(new_values)
            assert(len(self._values[i]) < self._max_categories)
            for k, v in self._maps[i].items():
                new_array[:, :, i][new_array[:, :, i] == k] = v
        return new_array

    @property
    def maps(self):
        map_list = [[i[0] for i in sorted(k.items(), key=lambda x: x[1])]
                    for k in self._maps]
        return map_list

    @property
    def sizes(self):
        size_list = [(len(k) if not maxed else None)
                     for maxed, k in zip(self._maxed_out, self._values)]
        return size_list



class _Statistics:
    """Class that computes online mean and variance."""

    def __init__(self, n_features: int) -> None:
        """Initialise the counters."""
        self._mean = np.zeros(n_features)
        self._m2 = np.zeros(n_features)
        self._n = np.zeros(n_features, dtype=int)

    def update(self, array: np.ma.MaskedArray) -> None:
        """Update calclulations with new data."""
        assert array.ndim == 2
        assert array.shape[0] > 1

        new_n = np.ma.count(array, axis=0)
        new_mean = np.ma.mean(array, axis=0)
        new_m2 = np.ma.var(array, axis=0, ddof=0) * new_n

        delta = new_mean - self._mean
        delta_mean = delta * (new_n / (new_n + self._n))

        self._mean += delta_mean
        self._m2 += new_m2 + (delta * self._n * delta_mean)
        self._n += new_n

    @property
    def mean(self) -> np.ndarray:
        """Get the current estimate of the mean."""
        assert np.all(self._n > 1)
        return self._mean

    @property
    def variance(self) -> np.ndarray:
        """Get the current estimate of the variance."""
        assert np.all(self._n > 1)
        var = self._m2 / self._n
        return var


def _to_masked(array: np.ndarray, missing_values: MissingValueList) \
        -> np.ma.MaskedArray:
    """Create a masked array from array plus list of missing."""
    assert len(missing_values) == array.shape[-1]
    mask = np.zeros_like(array, dtype=bool)
    for i, m in enumerate(missing_values):
        if m:
            mask[..., i] = array[..., i] == m
    marray = np.ma.MaskedArray(data=array, mask=mask)
    return marray


def write_datafile(image_stack: ImageStack, filename: str,
                   standardise: bool) -> None:
    """
    Write an ImageStack object to an HDF5 representation on disk.

    This function assumes writes iteratively from the image_stack,
    and therefore should support extremely large files.

    Parameters
    ----------
    image_stack : ImageStack
        The stack to write out (incrementally, need not fit on disk)
    filename : str
        The filename of the output HDF5 file.
    standardise : bool
        If true, rescale each ordinal feature to have mean 0 and std 1.

    """
    title = "Landshark Image Stack"
    log.info("Creating HDF5 output file")
    h5file = tables.open_file(filename, mode="w", title=title)

    # write the attributes to root
    log.info("Writing global attributes")
    attributes = h5file.root._v_attrs
    attributes.height = image_stack.height
    attributes.width = image_stack.width
    coords_x = image_stack.coordinates_x
    coords_y = image_stack.coordinates_y
    h5file.create_array(h5file.root, name="x_coordinates", obj=coords_x)
    h5file.create_array(h5file.root, name="y_coordinates", obj=coords_y)

    nbands_cat = len(image_stack.categorical_bands)
    nbands_ord = len(image_stack.ordinal_bands)
    cat_atom = tables.Int32Atom(shape=(nbands_cat,))
    ord_atom = tables.Float32Atom(shape=(nbands_ord,))
    filters = tables.Filters(complevel=1, complib="blosc:lz4")

    log.info("Creating data arrays")
    im_shape = (image_stack.height, image_stack.width)
    cat_array = h5file.create_carray(h5file.root, name="categorical_data",
                                     atom=cat_atom, shape=im_shape,
                                     filters=filters)
    cat_array.attrs.labels = image_stack.categorical_names
    ord_array = h5file.create_carray(h5file.root, name="ordinal_data",
                                     atom=ord_atom, shape=im_shape,
                                     filters=filters)
    ord_array.attrs.labels = image_stack.ordinal_names
    ord_array.attrs.missing_values = image_stack.ordinal_missing

    log.info("Categorical HDF5 block shape: {}".format(cat_array.chunkshape))
    log.info("Ordinal HDF5 block shape: {}".format(ord_array.chunkshape))

    # Default is to not store statistics
    ord_array.attrs.mean = None
    ord_array.attrs.variance = None

    log.info("Writing categorical data")
    cat_maps = _categorical_write(cat_array, image_stack.categorical_blocks,
                                  image_stack.categorical_missing)
    cat_array.attrs.mappings = cat_maps
    cat_array.attrs.ncategories = [len(k) for k in cat_maps]
    # Our encoding maps missing_values to zero
    cat_array.attrs.missing_values = [np.int32(0) for _ in
                                      image_stack.categorical_missing]

    log.info("Writing ordinal data")
    if standardise:

        mean, var = _get_stats(ord_array, image_stack.ordinal_blocks,
                               image_stack.ordinal_missing)
        ord_array.attrs.mean = mean
        ord_array.attrs.variance = var
        _standardise_write(ord_array, image_stack.ordinal_blocks,
                           image_stack.ordinal_missing, mean, var)
    else:
        _write(ord_array, image_stack.ordinal_blocks)

    log.info("Closing file")
    h5file.close()
    file_size = os.path.getsize(filename) // (1024 ** 2)
    log.info("Written {}MB file to disk.".format(file_size))


def _get_stats(array: tables.CArray,
               blocks: Callable[[], Iterator[np.ndarray]],
               missing_values: MissingValueList) \
        -> Tuple[np.ndarray, np.ndarray]:
    """Compute the mean and variance of the data."""
    nbands = array.atom.shape[0]
    stats = _Statistics(nbands)
    log.info("Computing statistics for standardisation")
    for b in blocks():
        bs = b.reshape((-1, nbands))
        bm = _to_masked(bs, missing_values)
        stats.update(bm)
    return stats.mean, stats.variance


def _standardise_write(array: tables.CArray,
                       blocks: Callable[[], Iterator[np.ndarray]],
                       missing_values: MissingValueList,
                       mean: np.ndarray,
                       variance: np.ndarray) -> None:
    """Write out standardised data."""
    start_idx = 0
    log.info("Writing standardised data")
    for b in blocks():
        end_idx = start_idx + b.shape[0]
        bm = _to_masked(b, missing_values)
        bm -= mean
        bm /= np.sqrt(variance)
        array[start_idx:end_idx] = bm.data
        start_idx = end_idx

def _categorical_write(array: tables.CArray,
           blocks: Callable[[], Iterator[np.ndarray]],
                       missing_values: MissingValueList):
    """Write without standardising."""
    cats = _Categories(missing_values)
    start_idx = 0
    for b in blocks():
        new_b = cats.update(b)
        end_idx = start_idx + b.shape[0]
        array[start_idx:end_idx] = new_b
        start_idx = end_idx
    return cats.maps

def _write(array: tables.CArray,
           blocks: Callable[[], Iterator[np.ndarray]]) -> None:
    """Write without standardising."""
    start_idx = 0
    for b in blocks():
        end_idx = start_idx + b.shape[0]
        array[start_idx:end_idx] = b
        start_idx = end_idx
