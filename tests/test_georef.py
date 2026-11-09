import math

import numpy as np
import pytest

from pointersearch.camera import focal_px, gsd_m
from pointersearch.config import CameraDefaults
from pointersearch.georef import Georeferencer, LocalFrame, rotation_cam_to_ned
from pointersearch.metadata import ImageMeta


def meta(**kw):
    base = dict(path="x.jpg", width=4000, height=3000, lat=-33.7, lon=150.3, rel_alt=100.0,
                gimbal_yaw=0.0, gimbal_pitch=-90.0, gimbal_roll=0.0, focal_35mm=24.0, source="wide")
    base.update(kw)
    return ImageMeta(**base)


def test_rotation_nadir_points_down():
    R = rotation_cam_to_ned(0, -90, 0)
    assert np.allclose(R @ np.array([0, 0, 1.0]), [0, 0, 1.0], atol=1e-9)   # optical axis -> down
    assert np.allclose(R @ np.array([1.0, 0, 0]), [0, 1.0, 0], atol=1e-9)   # image right -> east
    assert np.allclose(R @ np.array([0, 1.0, 0]), [-1.0, 0, 0], atol=1e-9)  # image down -> south


def test_rotation_horizon_yaw_east():
    R = rotation_cam_to_ned(90, 0, 0)
    assert np.allclose(R @ np.array([0, 0, 1.0]), [0, 1.0, 0], atol=1e-9)   # looking east


def test_nadir_centre_pixel_is_camera_position():
    m = meta()
    g = Georeferencer(m, CameraDefaults())
    lat, lon = g.pixel_to_latlon(m.width / 2, m.height / 2)
    assert abs(lat - m.lat) < 1e-9 and abs(lon - m.lon) < 1e-9


def test_footprint_matches_fov_and_gsd():
    m = meta()
    g = Georeferencer(m, CameraDefaults())
    fp = g.footprint_latlon()
    frame = LocalFrame(m.lat, m.lon)
    tl, tr, br, bl = (frame.to_ne(*c) for c in fp)
    width_m = tr[1] - tl[1]
    height_m = tl[0] - bl[0]
    f = focal_px(m, CameraDefaults())
    assert width_m == pytest.approx(m.rel_alt * m.width / f, rel=1e-6)
    assert height_m == pytest.approx(m.rel_alt * m.height / f, rel=1e-6)
    assert g.ground_gsd() == pytest.approx(gsd_m(m, CameraDefaults()), rel=1e-3)
    # top of image faces north when yaw = 0
    assert tl[0] > bl[0]


def test_yaw_rotates_footprint():
    m = meta(gimbal_yaw=90.0)
    g = Georeferencer(m, CameraDefaults())
    frame = g.frame
    tl = frame.to_ne(*g.pixel_to_latlon(0, 0))
    tr = frame.to_ne(*g.pixel_to_latlon(m.width, 0))
    # With yaw 90 the image top edge faces east, so TL -> TR runs north to south
    assert tl[1] == pytest.approx(tr[1], abs=1e-6)
    assert tl[0] > tr[0]


def test_oblique_pixel_above_horizon_is_none():
    m = meta(gimbal_pitch=-10.0)
    g = Georeferencer(m, CameraDefaults())
    assert g.pixel_to_latlon(m.width / 2, 0) is None       # top of frame sees sky
    assert g.pixel_to_latlon(m.width / 2, m.height) is not None
    assert g.footprint_latlon() is None


def test_gsd_scales_with_altitude():
    cam = CameraDefaults()
    assert gsd_m(meta(rel_alt=100), cam) == pytest.approx(2 * gsd_m(meta(rel_alt=50), cam))
    # Matrice 4T wide camera at 100 m is roughly 2 cm/px for a 48 MP frame
    assert 0.01 < gsd_m(meta(width=8064, height=6048, rel_alt=100), cam) < 0.03
