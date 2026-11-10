from gvpn_bench.results.render import CSS, WRAP_AFTER_CHARS, table


def test_short_cells_do_not_wrap_and_long_ones_do() -> None:
    long_text = "x " * WRAP_AFTER_CHARS
    html = table(["a", "b"], [["8.19", long_text]])
    assert "<td>8.19</td>" in html
    assert '<td class="wide">' in html
    assert '<div class="scroll">' in html


def test_cells_cannot_break_mid_word() -> None:
    assert "overflow-wrap:anywhere" not in CSS
    assert "td{white-space:nowrap}" in CSS
