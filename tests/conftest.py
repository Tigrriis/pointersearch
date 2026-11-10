import pytest

from pointersearch.config import Config
from pointersearch.pipeline import Pipeline

from pointersearch.devtools.synthetic import generate_mission


@pytest.fixture(scope="session")
def mission(tmp_path_factory):
    """A small synthetic mission shared by the tests (generation takes a few seconds)."""
    root = tmp_path_factory.mktemp("mission")
    images = root / "images"
    manifest = generate_mission(images, area_n_m=120, area_e_m=160, rgb_size=(1200, 900))
    return {"root": root, "images": images, "manifest": manifest}


@pytest.fixture(scope="session")
def canopy(tmp_path_factory):
    """Recorded with 3 m GPS error, 2 degrees heading error, and a surface 30 m above
    take-off level (so every photo is first laid down about 37 % too large)."""
    root = tmp_path_factory.mktemp("canopy")
    man = generate_mission(root / "DCIM", area_n_m=150, area_e_m=200, rgb_size=(1200, 900), detail=True,
                           pose_noise=(3.0, 2.0), alt_bias_m=30.0)
    wd = root / "mission"
    wd.mkdir()
    Config().save(wd)
    p = Pipeline(wd)
    p.process_path(root / "DCIM")
    before = {c["id"]: (c["lat"], c["lon"]) for c in p.store.list_candidates()}
    stats = p.align_mission()
    return {"wd": wd, "pipeline": p, "manifest": man, "stats": stats, "before": before, "images": root / "DCIM"}
