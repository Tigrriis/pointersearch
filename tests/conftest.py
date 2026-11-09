import pytest

from pointersearch.devtools.synthetic import generate_mission


@pytest.fixture(scope="session")
def mission(tmp_path_factory):
    """A small synthetic mission shared by the tests (generation takes a few seconds)."""
    root = tmp_path_factory.mktemp("mission")
    images = root / "images"
    manifest = generate_mission(images, area_n_m=120, area_e_m=160, rgb_size=(1200, 900))
    return {"root": root, "images": images, "manifest": manifest}
