import cv2
import numpy as np

from pointersearch.config import BrightColourConfig, RXConfig, ThermalConfig
from pointersearch.detectors import BrightColourDetector, RXDetector, ThermalDetector


def scrub(h=600, w=800, seed=1):
    rng = np.random.default_rng(seed)
    img = np.empty((h, w, 3), np.uint8)
    img[:] = (70, 95, 118)
    img = np.clip(img.astype(np.float32) + rng.normal(0, 5, img.shape), 0, 255).astype(np.uint8)
    return img


def test_rx_finds_blue_patch_and_little_else():
    img = scrub()
    img[300:312, 400:412] = (200, 60, 20)       # 12x12 px blue object
    cands = RXDetector(RXConfig()).detect(img, gsd_m=0.1)
    assert cands, "blue patch not detected"
    top = cands[0]
    assert abs(top.px - 406) < 3 and abs(top.py - 306) < 3
    assert len(cands) <= 2


def test_rx_quiet_on_plain_texture():
    cands = RXDetector(RXConfig()).detect(scrub(seed=3), gsd_m=0.1)
    assert len(cands) <= 1


def test_bright_detects_orange_and_ignores_green():
    img = scrub()
    img[100:110, 100:110] = (10, 110, 245)      # orange
    img[400:410, 600:610] = (40, 200, 40)       # saturated green (vegetation)
    cands = BrightColourDetector(BrightColourConfig()).detect(img, gsd_m=0.1)
    assert len(cands) == 1
    assert abs(cands[0].px - 105) < 3


def test_thermal_hotspot():
    rng = np.random.default_rng(0)
    gray = np.clip(rng.normal(105, 2, (512, 640)), 0, 255).astype(np.uint8)
    gray[250:260, 300:309] = 235
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    cands = ThermalDetector(ThermalConfig()).detect(bgr, gsd_m=0.1)
    assert len(cands) == 1
    c = cands[0]
    assert abs(c.px - 304) < 2 and abs(c.py - 254.5) < 2
    assert 0.5 < c.area_m2 < 1.5


def test_thermal_red_highlight_palette_ranks_red_as_hottest():
    # DJI white-hot palettes that paint the hottest band red: red must beat white, not lose to grey
    from pointersearch.detectors.thermal import heat_field
    rng = np.random.default_rng(0)
    base = np.clip(rng.normal(105, 2, (512, 640)), 0, 255).astype(np.uint8)
    bgr = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    bgr[100:110, 100:109] = (235, 235, 235)                 # warm: near white
    bgr[300:310, 400:409] = (30, 30, 235)                   # hottest: painted red
    bgr = cv2.imdecode(cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])[1], cv2.IMREAD_COLOR)
    field = heat_field(bgr)
    assert field[305, 404] > field[105, 104] > field[200, 200]
    assert cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)[305, 404] < 105          # what plain grey would have seen
    cands = ThermalDetector(ThermalConfig()).detect(bgr, gsd_m=0.1)
    assert len(cands) == 2
    assert abs(cands[0].px - 404) < 3 and abs(cands[0].py - 304.5) < 3    # the red one ranks first
    assert abs(cands[1].px - 104) < 3
    assert heat_field(base).dtype == np.float32 and heat_field(base).shape == base.shape


def test_thermal_size_filter_rejects_large_warm_area():
    gray = np.full((512, 640), 105, np.uint8)
    gray[100:400, 100:500] = 200                 # sun-warmed slab, far bigger than a person
    cands = ThermalDetector(ThermalConfig()).detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), gsd_m=0.1)
    assert cands == []
