"""Opt-in live checks against the real Palatial API. Run with: uv run pytest -m live"""

import pytest

from usd_pipeline.config import Settings
from usd_pipeline.palatial import PalatialClient

KNOWN_ID = "69fdc4a58102b48e9ae52b8d"


@pytest.mark.live
def test_search_live():
    s = Settings(_env_file=None)
    with PalatialClient(base_url=s.palatial_base_url) as c:
        data = c.search(q="", page=1, limit=2)
    assert "results" in data and len(data["results"]) >= 1


@pytest.mark.live
def test_mint_and_detail_live():
    s = Settings(_env_file=None)
    with PalatialClient(base_url=s.palatial_base_url) as c:
        token = c.mint_viewer_cookie(KNOWN_ID)
        detail = c.asset_detail(KNOWN_ID, cookie=token)
    assert detail.get("_id") == KNOWN_ID or detail.get("name")


@pytest.mark.live
def test_process_file_live():
    s = Settings(_env_file=None)
    with PalatialClient(base_url=s.palatial_base_url) as c:
        data = c.process_file(KNOWN_ID, "shape-generation")
    assert data.get("downloadUrl")
