from cpc_parser import CpcFile, CpcParseError


def make_cpc(line1='"codes.txt","D:\\Survey\\reef.jpg",12000,9000,800,600'):
    lines = [line1, "0,0", "0,9000", "12000,9000", "12000,0", "1", "1500,1500", '"1","ACR","Notes",""']
    return "\r\n".join(lines) + "\r\n"


def test_parse_minimal_cpc():
    cpc = CpcFile.parse(make_cpc())
    assert cpc.embedded_image_name == "reef.jpg"
    assert cpc.image_width == "12000"
    assert len(cpc.points) == 1
    assert cpc.points[0].label_id == "ACR"


def test_round_trip():
    cpc = CpcFile.parse(make_cpc())
    parsed = CpcFile.parse(cpc.to_text())
    assert parsed.image_filepath == cpc.image_filepath
    assert parsed.points[0].x == "1500"


def test_bad_line_one():
    try:
        CpcFile.parse(make_cpc('"only-one"'))
    except CpcParseError as exc:
        assert "6 were expected" in str(exc)
    else:
        raise AssertionError("Expected CpcParseError")
