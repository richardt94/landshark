"""Exceptions and errors from CLI and internal processing."""

import logging
import sys
from typing import Any, Callable, List

import numpy as np

log = logging.getLogger(__name__)


class Error(Exception):
    """Base class for exceptions in Landshark."""

    message = "Landshark error."
    pass


def catch_and_exit(f: Callable) -> Callable:
    """Decorate function to exit program if it throws an Error."""
    def wrapped(*args: Any, **kwargs: Any) -> None:
        try:
            f(*args, **kwargs)
        except Error as e:
            log.error(e.message)
            sys.exit()

    return wrapped


class NoTifFilesFound(Error):
    """Couldn't find TIF files."""

    message = "The supplied paths had no files with .tif or .gtif extensions"


class ZeroVariance(Error):
    """Zero variance in supplied column."""

    def __init__(self, var: np.ndarray, cols: List[str]) -> None:
        zsrcs = [c for z, c in zip(var, cols) if z]
        self.message = "The following sources have bands \
            with zero variance: {}".format(zsrcs)


class OrdCatNMismatch(Error):
    """N doesnt match between the ord and cat sources."""

    def __init__(self, N_ord: int, N_cat: int) -> None:
        """Construct the object."""
        self.message = "Ordinal and Categorical source mismatch with \
            {} and {} points respectively".format(N_ord, N_cat)
