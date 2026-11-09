"""Candidate detectors. Each takes a full-resolution image and a ground sample
distance and returns a list of :class:`Candidate` in full-resolution pixel coords."""

from .base import Candidate, blobs_from_mask, crop_thumbnail
from .bright import BrightColourDetector
from .rx import RXDetector
from .thermal import ThermalDetector

__all__ = [
    "Candidate",
    "blobs_from_mask",
    "crop_thumbnail",
    "BrightColourDetector",
    "RXDetector",
    "ThermalDetector",
]
