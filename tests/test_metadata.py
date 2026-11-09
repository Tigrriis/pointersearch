from pointersearch.metadata import read_image_meta, read_xmp_dji


def test_xmp_parse_attributes_and_elements():
    packet = (b'<x:xmpmeta><rdf:Description drone-dji:RelativeAltitude="+80.300" '
              b'drone-dji:GimbalYawDegree="-12.50"><drone-dji:GpsLatitude>-33.5</drone-dji:GpsLatitude>'
              b'</rdf:Description></x:xmpmeta>')
    d = read_xmp_dji(b"\xff\xd8junk" + packet + b"tail")
    assert d["RelativeAltitude"] == "+80.300"
    assert d["GimbalYawDegree"] == "-12.50"
    assert d["GpsLatitude"] == "-33.5"


def test_read_synthetic_images(mission):
    files = sorted(mission["images"].glob("*.JPG"))
    wide = [f for f in files if f.stem.endswith("_W")]
    thermal = [f for f in files if f.stem.endswith("_T")]
    assert wide and thermal
    m = read_image_meta(wide[0])
    assert m.source == "wide" and not m.is_thermal
    assert m.rel_alt == 80.0 and m.gimbal_pitch == -90.0
    assert m.focal_35mm == 24
    assert m.width == 1200 and m.height == 900
    assert m.captured_at.startswith("2025-06-01")
    t = read_image_meta(thermal[0])
    assert t.is_thermal and t.focal_35mm == 53
