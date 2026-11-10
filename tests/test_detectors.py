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


def _thermal_scene():
    """Grey bush with the palette colours measured on real Matrice 4T frames: a pinkish-red
    hottest band, here as a core in the middle of sun-warmed dead branches. The branches are
    thin, bright and all joined, so together they are far too big to be a person."""
    rng = np.random.default_rng(1)
    base = np.clip(rng.normal(90, 6, (1024, 1280)), 0, 255).astype(np.uint8)
    bgr = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    for ang in range(0, 360, 30):                                                  # warm branches, ~17 m across
        end = (int(430 + 210 * np.cos(np.radians(ang))), int(520 + 210 * np.sin(np.radians(ang))))
        cv2.line(bgr, (430, 520), end, (215, 215, 215), 9)
    cv2.ellipse(bgr, (430, 520), (11, 24), 10, 0, 360, (110, 108, 209), -1)        # red-hot core where they meet
    cv2.ellipse(bgr, (980, 300), (8, 14), 0, 0, 360, (110, 108, 209), -1)          # red-hot, on its own
    bgr[800:802, 1100:1102] = (110, 108, 209)                                      # a speck: too small to count
    return cv2.imdecode(cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])[1], cv2.IMREAD_COLOR)


def test_red_hot_core_inside_a_large_warm_region_is_kept():
    scene, gsd = _thermal_scene(), 0.04
    near = lambda c, x, y: abs(c.px - x) < 6 and abs(c.py - y) < 6

    # without the red-band search the branches and core are one big warm object, discarded whole
    grey_only = ThermalDetector(ThermalConfig(red_hot=False)).detect(scene, gsd)
    assert not any(near(c, 430, 520) for c in grey_only)

    found = ThermalDetector(ThermalConfig()).detect(scene, gsd)
    hot = [c for c in found if c.extra.get("red_hot")]
    assert len(hot) == 2 and found[:2] == hot                      # both red areas, listed first
    assert near(hot[0], 430, 520)                                  # the larger area leads
    assert any(near(c, 430, 520) for c in hot) and any(near(c, 980, 300) for c in hot)
    core = next(c for c in hot if near(c, 430, 520))
    assert 0.8 < core.area_m2 < 2.0                                # the core's own size, not the branches'
    assert not any(near(c, 1101, 801) for c in found)              # the speck is ignored
    # the same spot is not reported twice (once as red, once as the warm blob around it)
    assert sum(1 for c in found if abs(c.px - 980) < 20 and abs(c.py - 300) < 20) == 1


def test_red_mask_matches_the_measured_palette_and_not_ordinary_greys():
    from pointersearch.detectors.thermal import red_mask
    px = np.zeros((1, 6, 3), np.uint8)
    px[0] = [(110, 108, 209), (50, 49, 144), (163, 162, 246), (200, 200, 200), (255, 255, 255), (120, 118, 150)]
    assert red_mask(px)[0].tolist() == [True, True, True, False, False, False]
    assert not red_mask(np.zeros((4, 4), np.uint8)).any()


def test_thermal_size_filter_rejects_large_warm_area():
    gray = np.full((512, 640), 105, np.uint8)
    gray[100:400, 100:500] = 200                 # sun-warmed slab, far bigger than a person
    cands = ThermalDetector(ThermalConfig()).detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), gsd_m=0.1)
    assert cands == []
