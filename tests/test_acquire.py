import signal

import httpx
import pytest
import respx

from usd_pipeline.acquire import (
    RUN_HDR_URL,
    _verify_length,
    filename_from_url,
    make_run_id,
    run_acquisition,
)
from usd_pipeline.config import Settings
from usd_pipeline.palatial import PalatialError
from usd_pipeline.storage import LocalStore

BASE = "https://api.example.test/v1"
PROCESS_KINDS = [
    "shape-generation",
    "texture",
    "collision-preview",
    "physics-predictions",
    "validation-playback",
]


def _settings():
    return Settings(_env_file=None, palatial_base_url=BASE, http_max_retries=2)


def _mock_asset(asset_id: str, *, texture_has_file: bool = True, detail_status: int = 200) -> None:
    respx.post(f"{BASE}/share/library-viewer-session").mock(
        return_value=httpx.Response(
            201,
            json={"success": True},
            headers={"set-cookie": "asset_share_viewer=JWT; Path=/"},
        )
    )
    detail_body = {} if detail_status == 401 else {"_id": asset_id, "name": "thing"}
    respx.get(f"{BASE}/assets/{asset_id}").mock(
        return_value=httpx.Response(detail_status, json=detail_body)
    )
    for kind in PROCESS_KINDS:
        if kind == "texture" and not texture_has_file:
            body = {"state": "not_found"}
        else:
            body = {
                "state": "found",
                "downloadUrl": f"https://dl/{asset_id}/{kind}/file_{kind}.glb",
            }
        respx.get(f"{BASE}/assets/{asset_id}/process-file/{kind}").mock(
            return_value=httpx.Response(200, json=body)
        )
    respx.get(f"{BASE}/assets/{asset_id}/embedded/physics").mock(
        return_value=httpx.Response(200, json={"parts": []})
    )
    respx.get(f"{BASE}/assets/{asset_id}/media/validation-report").mock(
        return_value=httpx.Response(200, json={"downloadUrl": f"https://dl/{asset_id}/report.json"})
    )
    respx.route(host="dl").mock(return_value=httpx.Response(200, content=b"BINARY"))
    respx.get(RUN_HDR_URL).mock(return_value=httpx.Response(200, content=b"HDRDATA"))


def test_make_run_id_format():
    rid = make_run_id()
    assert rid.endswith("Z") and "T" in rid and ":" not in rid


@respx.mock
def test_acquire_asset_single(tmp_path):
    from usd_pipeline.acquire import acquire_asset

    _mock_asset("a1")  # no library/search needed — acquire_asset targets one id
    store = LocalStore(tmp_path)
    m = acquire_asset("a1", _settings(), store=store)
    rid = m.data["run_id"]
    assert set(m.asset_ids()) == {"a1"}
    assert store.exists(f"runs/{rid}/a1/detail.json")
    assert store.exists(f"runs/{rid}/a1/texture/file_texture.glb")
    assert m.data["assets"]["a1"]["steps"]["embedded-physics"]["status"] == "done"
    assert m.stats()["steps_failed"] == 0


def test_filename_from_url():
    assert filename_from_url("https://x/y/shape_69.glb?sig=abc") == "shape_69.glb"
    assert filename_from_url("https://x/") == "artifact.bin"


@respx.mock
def test_run_acquisition_happy_path(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1", "asset_name": "A"}]})
    )
    _mock_asset("a1")
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")

    prefix = "runs/r1"
    assert store.exists(f"{prefix}/library-search.json")
    assert store.exists(f"{prefix}/manifest.json")
    assert store.exists(f"{prefix}/a1/detail.json")
    assert store.exists(f"{prefix}/a1/texture.response.json")
    assert store.exists(f"{prefix}/a1/texture/file_texture.glb")
    assert store.exists(f"{prefix}/a1/embedded-physics.json")
    assert store.exists(f"{prefix}/a1/validation-report.response.json")
    assert store.exists(f"{prefix}/a1/validation-report/report.json")
    stats = m.stats()
    assert stats["steps_failed"] == 0
    assert stats["steps_done"] == 8


@respx.mock
def test_absent_process_file_is_not_failure(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1", texture_has_file=False)
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")
    assert store.exists("runs/r1/a1/texture.response.json")
    assert not store.exists("runs/r1/a1/texture/file_texture.glb")
    assert m.data["assets"]["a1"]["steps"]["texture"]["status"] == "absent"
    assert m.stats()["steps_failed"] == 0


@respx.mock
def test_detail_401_recorded_failed_not_fatal(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1", detail_status=401)
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")
    assert m.data["assets"]["a1"]["steps"]["detail"]["status"] == "failed"
    assert m.data["assets"]["a1"]["steps"]["detail"]["http_status"] == 401
    assert m.data["assets"]["a1"]["steps"]["embedded-physics"]["status"] == "done"


def test_resume_skips_done_steps(tmp_path):
    store = LocalStore(tmp_path)
    with respx.mock:
        respx.get(f"{BASE}/library/search").mock(
            return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
        )
        _mock_asset("a1")
        run_acquisition(_settings(), store=store, run_id="r1")

    # Fresh router: everything errors. Resume must not touch the network.
    with respx.mock:
        search_route = respx.get(f"{BASE}/library/search").mock(return_value=httpx.Response(500))
        detail_route = respx.get(f"{BASE}/assets/a1").mock(return_value=httpx.Response(500))
        m = run_acquisition(_settings(), store=store, resume_run_id="r1")

    assert search_route.call_count == 0
    assert detail_route.call_count == 0
    assert m.stats()["steps_failed"] == 0


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def test_verify_length_ok():
    _verify_length(_Resp({"content-length": "6"}), 6)  # no raise


def test_verify_length_mismatch_raises():
    with pytest.raises(PalatialError):
        _verify_length(_Resp({"content-length": "999"}), 6)


def test_verify_length_skips_when_content_encoded():
    _verify_length(_Resp({"content-length": "999", "content-encoding": "gzip"}), 6)  # no raise


def test_verify_length_no_header_ok():
    _verify_length(_Resp({}), 123)  # no raise


@respx.mock
def test_download_failure_marks_step_failed_not_fatal(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    # Register the failing artifact route BEFORE _mock_asset's host="dl" catch-all so it wins
    # (respx matches routes in registration order). The shape-generation download drops
    # mid-transfer (a transient protocol error).
    respx.get("https://dl/a1/shape-generation/file_shape-generation.glb").mock(
        side_effect=httpx.RemoteProtocolError("connection dropped")
    )
    _mock_asset("a1")
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")
    assert m.data["assets"]["a1"]["steps"]["shape-generation"]["status"] == "failed"
    # one failed download does not abort the asset — other steps still succeed
    assert m.data["assets"]["a1"]["steps"]["embedded-physics"]["status"] == "done"
    assert m.data["assets"]["a1"]["steps"]["texture"]["status"] == "done"


@respx.mock
def test_no_signal_handler_installed_by_default(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1")
    before = signal.getsignal(signal.SIGINT)
    run_acquisition(_settings(), store=LocalStore(tmp_path), run_id="r1")
    assert signal.getsignal(signal.SIGINT) is before


@respx.mock
def test_signal_handler_restored_when_enabled(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1")
    before = signal.getsignal(signal.SIGINT)
    run_acquisition(
        _settings(), store=LocalStore(tmp_path), run_id="r1", install_signal_handlers=True
    )
    assert signal.getsignal(signal.SIGINT) is before  # restored in finally


@respx.mock
def test_run_hdr_saved_once_per_run(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1")
    store = LocalStore(tmp_path)
    run_acquisition(_settings(), store=store, run_id="r1")
    assert store.exists("runs/r1/studio_country_hall_1k-3e5b7ed8.hdr")


@respx.mock
def test_validation_report_404_is_absent_not_failed(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    _mock_asset("a1")
    # Re-mock the same URL AFTER _mock_asset so this 404 overrides the helper's 200.
    respx.get(f"{BASE}/assets/a1/media/validation-report").mock(
        return_value=httpx.Response(
            404, json={"message": "Validation report not found", "error": "Not Found"}
        )
    )
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")
    assert m.data["assets"]["a1"]["steps"]["validation-report"]["status"] == "absent"
    assert m.stats()["steps_failed"] == 0


@respx.mock
def test_forbidden_asset_steps_are_skipped_not_failed(tmp_path):
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    body = {"message": "Asset is not available in the library", "error": "Forbidden"}
    respx.post(f"{BASE}/share/library-viewer-session").mock(
        return_value=httpx.Response(
            201, json={}, headers={"set-cookie": "asset_share_viewer=J; Path=/"}
        )
    )
    respx.get(f"{BASE}/assets/a1").mock(return_value=httpx.Response(403, json=body))
    for kind in PROCESS_KINDS:
        respx.get(f"{BASE}/assets/a1/process-file/{kind}").mock(
            return_value=httpx.Response(403, json=body)
        )
    respx.get(f"{BASE}/assets/a1/embedded/physics").mock(
        return_value=httpx.Response(403, json=body)
    )
    respx.get(f"{BASE}/assets/a1/media/validation-report").mock(
        return_value=httpx.Response(403, json=body)
    )
    respx.get(RUN_HDR_URL).mock(return_value=httpx.Response(200, content=b"HDR"))
    store = LocalStore(tmp_path)
    m = run_acquisition(_settings(), store=store, run_id="r1")
    steps = m.data["assets"]["a1"]["steps"]
    assert all(s["status"] == "skipped" for s in steps.values()), steps
    stats = m.stats()
    assert stats["steps_failed"] == 0
    assert stats["steps_skipped"] == 8
