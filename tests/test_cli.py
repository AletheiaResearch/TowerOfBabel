import json

import httpx
import respx
from typer.testing import CliRunner

from usd_pipeline.cli import app

BASE = "https://api.example.test/v1"
PROCESS_KINDS = [
    "shape-generation",
    "texture",
    "collision-preview",
    "physics-predictions",
    "validation-playback",
]
runner = CliRunner()


def _env(monkeypatch):
    monkeypatch.setenv("PALATIAL_BASE_URL", BASE)
    monkeypatch.setenv("PIPELINE_CONCURRENCY", "2")


def _mock_single_asset():
    respx.get(f"{BASE}/library/search").mock(
        return_value=httpx.Response(200, json={"results": [{"_id": "a1"}]})
    )
    respx.post(f"{BASE}/share/library-viewer-session").mock(
        return_value=httpx.Response(
            201, json={}, headers={"set-cookie": "asset_share_viewer=J; Path=/"}
        )
    )
    respx.get(f"{BASE}/assets/a1").mock(return_value=httpx.Response(200, json={"_id": "a1"}))
    for kind in PROCESS_KINDS:
        respx.get(f"{BASE}/assets/a1/process-file/{kind}").mock(
            return_value=httpx.Response(200, json={"state": "not_found"})
        )
    respx.get(f"{BASE}/assets/a1/embedded/physics").mock(
        return_value=httpx.Response(200, json={"x": 1})
    )
    respx.get(f"{BASE}/assets/a1/media/validation-report").mock(
        return_value=httpx.Response(200, json={})
    )


@respx.mock
def test_acquire_local(monkeypatch, tmp_path):
    _env(monkeypatch)
    _mock_single_asset()
    out_dir = tmp_path / "out"
    result = runner.invoke(
        app, ["acquire", "--local", str(out_dir), "--run-id", "r1", "--page-limit", "1"]
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "runs/r1/manifest.json").exists()
    assert (out_dir / "runs/r1/a1/detail.json").exists()


def test_status_missing_run(tmp_path):
    result = runner.invoke(app, ["status", "rX", "--local", str(tmp_path)])
    assert result.exit_code != 0


def test_compose_local(monkeypatch, tmp_path):
    from pathlib import Path

    from usd_pipeline import compose_run
    from usd_pipeline.manifest import Manifest
    from usd_pipeline.models import StepOutcome, StepStatus
    from usd_pipeline.storage import LocalStore

    def _fake_compose(textured, collision, physics, out_path, **kw):
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("#usda 1.0\n")
        usdz = out.with_suffix(".usdz")
        usdz.write_bytes(b"USDZ")
        return {"usd": str(out), "texture": None, "usdz": str(usdz)}

    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)

    r2 = tmp_path / "r2"
    store = LocalStore(r2)
    prefix = "runs/rc"
    m = Manifest.create(
        store,
        run_id="rc",
        prefix=prefix,
        bucket="local",
        results=[{"_id": "a1"}],
        source_url="x",
        reference_key=f"{prefix}/library-search.json",
        manifest_key=f"{prefix}/manifest.json",
    )
    tex = f"{prefix}/a1/texture/t.glb"
    col = f"{prefix}/a1/collision-preview/c.glb"
    phy = f"{prefix}/a1/embedded-physics.json"
    store.put_bytes(tex, b"T")
    store.put_bytes(col, b"C")
    store.put_json(phy, {"parts": [{"is_structural_root": True}]})
    m.update_step("a1", "texture", StepOutcome(StepStatus.DONE, keys={"artifact": tex}))
    m.update_step("a1", "collision-preview", StepOutcome(StepStatus.DONE, keys={"artifact": col}))
    m.update_step("a1", "embedded-physics", StepOutcome(StepStatus.DONE, keys={"file": phy}))
    m.flush()

    usd_dir = tmp_path / "usd"
    result = runner.invoke(
        app, ["compose", "rc", "--local", str(r2), "--usd-dir", str(usd_dir), "--concurrency", "1"]
    )
    assert result.exit_code == 0, result.output
    stats = json.loads(result.output[result.output.index("{") :])  # skip any progress output
    assert stats["done"] == 1
    assert (usd_dir / "a1" / "a1.usdz").exists()
    assert store.exists("usd/a1/a1.usdz")


@respx.mock
def test_status_reports_stats(monkeypatch, tmp_path):
    _env(monkeypatch)
    _mock_single_asset()
    out_dir = tmp_path / "out"
    runner.invoke(app, ["acquire", "--local", str(out_dir), "--run-id", "r1", "--page-limit", "1"])

    result = runner.invoke(app, ["status", "r1", "--local", str(out_dir)])
    assert result.exit_code == 0
    stats = json.loads(result.output)
    assert stats["assets_total"] == 1
