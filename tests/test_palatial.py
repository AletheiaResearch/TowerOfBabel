import httpx
import pytest
import respx

from usd_pipeline.palatial import PalatialClient, PalatialHTTPError

BASE = "https://api.example.test/v1"


def _client():
    return PalatialClient(base_url=BASE, max_retries=2)


@respx.mock
def test_search():
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}, {"_id": "a2"}]})
    )
    with _client() as c:
        data = c.search(q="", page=1, limit=5)
    assert [r["_id"] for r in data["results"]] == ["a1", "a2"]


@respx.mock
def test_mint_viewer_cookie():
    respx.post(f"{BASE}/share/library-viewer-session").mock(
        return_value=httpx.Response(
            201,
            json={"success": True, "assetId": "a1"},
            headers={"set-cookie": "asset_share_viewer=JWT123; Path=/; HttpOnly"},
        )
    )
    with _client() as c:
        token = c.mint_viewer_cookie("a1")
    assert token == "JWT123"


@respx.mock
def test_asset_detail_sends_minted_cookie():
    route = respx.get(f"{BASE}/assets/a1").mock(
        return_value=httpx.Response(200, json={"_id": "a1", "name": "thing"})
    )
    with _client() as c:
        detail = c.asset_detail("a1", cookie="JWT123")
    assert detail["_id"] == "a1"
    assert "asset_share_viewer=JWT123" in route.calls.last.request.headers["cookie"]


@respx.mock
def test_asset_detail_uses_full_cookie_override():
    route = respx.get(f"{BASE}/assets/a1").mock(
        return_value=httpx.Response(200, json={"_id": "a1"})
    )
    c = PalatialClient(base_url=BASE, cookie="session=abc; other=def", max_retries=2)
    with c:
        c.asset_detail("a1")
    assert route.calls.last.request.headers["cookie"] == "session=abc; other=def"


@respx.mock
def test_asset_detail_401_raises():
    respx.get(f"{BASE}/assets/a1").mock(return_value=httpx.Response(401, json={}))
    with _client() as c, pytest.raises(PalatialHTTPError) as ei:
        c.asset_detail("a1", cookie="bad")
    assert ei.value.status_code == 401


@respx.mock
def test_process_file_returns_json():
    respx.get(f"{BASE}/assets/a1/process-file/texture").mock(
        return_value=httpx.Response(200, json={"state": "found", "downloadUrl": "https://dl/x.glb"})
    )
    with _client() as c:
        data = c.process_file("a1", "texture")
    assert data["downloadUrl"] == "https://dl/x.glb"


@respx.mock
def test_download_streams_bytes():
    respx.get("https://dl/x.glb").mock(return_value=httpx.Response(200, content=b"BINARYDATA"))
    with _client() as c, c.download("https://dl/x.glb") as resp:
        chunks = b"".join(resp.iter_bytes())
    assert chunks == b"BINARYDATA"


@respx.mock
def test_retries_on_500_then_succeeds():
    route = respx.get(f"{BASE}/assets/a1/embedded/physics").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    with _client() as c:
        data = c.embedded_physics("a1")
    assert data == {"ok": True}
    assert route.call_count == 2


@respx.mock
def test_retries_on_429_then_succeeds():
    route = respx.get(f"{BASE}/library/search").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"results": []})]
    )
    with _client() as c:
        data = c.search()
    assert data == {"results": []}
    assert route.call_count == 2


def test_redact_url_strips_signature():
    from usd_pipeline.palatial import _redact_url

    redacted = _redact_url("https://r2.example/path/file.glb?X-Amz-Signature=secret&x=1")
    assert redacted == "https://r2.example/path/file.glb"
    assert "secret" not in redacted
