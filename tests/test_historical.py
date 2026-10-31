"""Historical import, run only where the read-only ladder repos exist locally."""

import re
from pathlib import Path

import pytest

from gvpn_bench.results import historical
from gvpn_bench.results.render import render
from gvpn_bench.results.schema import validate

SERBIA = Path.home() / "gnosis-vpn-serbia-ladder"
LITHUANIA = Path.home() / "gnosis-vpn-lithuania-ladder"
PUBLIC_IPV4 = re.compile(r"\b(?!9\.9\.9\.9\b)(?:\d{1,3}\.){3}\d{1,3}\b")

pytestmark = pytest.mark.skipif(not (SERBIA.exists() and LITHUANIA.exists()), reason="ladder repos not present")


@pytest.mark.parametrize(
    "loader, repo",
    [
        (historical.import_serbia_v5, SERBIA),
        (historical.import_kaunas, LITHUANIA),
        (historical.import_v5_sweep, LITHUANIA),
        (historical.import_serbia_v4, LITHUANIA),
    ],
)
def test_import_is_valid_and_marked_not_reproducible(loader, repo):
    result = loader(repo)
    validate(result)
    assert result["run"]["reproducible"] is False
    if result["concurrent"]:
        assert all(r["total_mbps"] is None for r in result["concurrent"]["rungs"])
    html = render(result)
    assert not PUBLIC_IPV4.search(html.replace("172.30.0.1", ""))
