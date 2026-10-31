from gvpn_bench.measure import parse_probe_output


def test_probe_output_parses_colo_and_ttfb():
    out = "HTTP/2 200\r\ncf-ray: x\r\ncolo: FRA\r\n\r\n\n0.041234"
    assert parse_probe_output(out) == (41.2, "FRA")


def test_probe_output_without_headers():
    assert parse_probe_output("") == (None, None)
    assert parse_probe_output("garbage") == (None, None)
