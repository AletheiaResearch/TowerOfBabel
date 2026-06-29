"""Batch compose orchestration tests — compose_usd is monkeypatched (no real USD/GLB)."""

from pathlib import Path

from usd_pipeline import compose_run
from usd_pipeline.compose_run import ComposeLedger, _is_safe_segment, run_compose
from usd_pipeline.config import Settings
from usd_pipeline.manifest import Manifest
from usd_pipeline.models import StepOutcome, StepStatus
from usd_pipeline.storage import LocalStore


def _seed_one(store, aid, tex_key, col_key, tex=b"TEX", col=b"COL", run_id="r1"):
    """Seed a manifest with a single composable asset using the given input keys."""
    prefix = f"runs/{run_id}"
    m = Manifest.create(
        store,
        run_id=run_id,
        prefix=prefix,
        bucket="local",
        results=[{"_id": aid}],
        source_url="x",
        reference_key=f"{prefix}/library-search.json",
        manifest_key=f"{prefix}/manifest.json",
    )
    phy = f"{prefix}/{aid}/embedded-physics.json"
    store.put_bytes(tex_key, tex)
    store.put_bytes(col_key, col)
    store.put_json(phy, {"parts": [{"is_structural_root": True}]})
    m.update_step(aid, "texture", StepOutcome(StepStatus.DONE, keys={"artifact": tex_key}))
    m.update_step(
        aid, "collision-preview", StepOutcome(StepStatus.DONE, keys={"artifact": col_key})
    )
    m.update_step(aid, "embedded-physics", StepOutcome(StepStatus.DONE, keys={"file": phy}))
    m.flush()


def _settings():
    return Settings(_env_file=None)


def _seed_run(store, run_id="r1"):
    """Create an acquisition manifest + the three input objects for two assets."""
    prefix = f"runs/{run_id}"
    results = [{"_id": "a1", "asset_name": "Alpha"}, {"_id": "a2", "asset_name": "Beta"}]
    m = Manifest.create(
        store,
        run_id=run_id,
        prefix=prefix,
        bucket="local",
        results=results,
        source_url="http://s",
        reference_key=f"{prefix}/library-search.json",
        manifest_key=f"{prefix}/manifest.json",
    )
    # a1 has all three inputs; a2 is missing collision (not composable)
    tex = f"{prefix}/a1/texture/textured_mesh_a1.glb"
    col = f"{prefix}/a1/collision-preview/collision_hulls.glb"
    phy = f"{prefix}/a1/embedded-physics.json"
    store.put_bytes(tex, b"GLB-TEX")
    store.put_bytes(col, b"GLB-COL")
    store.put_json(phy, {"data": {"parts": [{"is_structural_root": True}]}})
    from usd_pipeline.models import StepOutcome, StepStatus

    m.update_step("a1", "texture", StepOutcome(StepStatus.DONE, keys={"artifact": tex}))
    m.update_step("a1", "collision-preview", StepOutcome(StepStatus.DONE, keys={"artifact": col}))
    m.update_step("a1", "embedded-physics", StepOutcome(StepStatus.DONE, keys={"file": phy}))
    m.update_step("a2", "texture", StepOutcome(StepStatus.DONE, keys={"artifact": "x"}))
    m.update_step("a2", "embedded-physics", StepOutcome(StepStatus.DONE, keys={"file": "y"}))
    m.flush()
    return prefix


def _fake_compose(textured, collision, physics, out_path, **kw):
    """Stand-in for compose_usd: writes dummy .usda/.usdz/png and returns their paths."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("#usda 1.0\n")
    png = out.with_name(out.stem + "_basecolor.png")
    png.write_bytes(b"PNG")
    usdz = out.with_suffix(".usdz") if kw.get("usdz") else None
    if usdz:
        usdz.write_bytes(b"USDZ")
    return {"usd": str(out), "texture": str(png), "usdz": str(usdz) if usdz else None}


def test_run_compose_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    prefix = _seed_run(store)
    usd_dir = tmp_path / "usd"

    ledger = run_compose(
        "r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1, usdz=True
    )

    stats = ledger.stats()
    assert stats["done"] == 1  # a1 composed
    assert stats["absent"] == 1  # a2 missing inputs
    # outputs in the global usd dir, keyed by asset id
    assert (usd_dir / "a1" / "a1.usda").exists()
    assert (usd_dir / "a1" / "a1.usdz").exists()
    assert (usd_dir / "a1" / "a1_basecolor.png").exists()
    # uploaded to R2 under the GLOBAL usd/ prefix (parallel to runs/), keyed by asset id
    assert store.exists("usd/a1/a1.usda")
    assert store.exists("usd/a1/a1.usdz")
    # compose ledger persisted under the run prefix
    assert store.exists(f"{prefix}/compose-manifest.json")
    assert ledger.data["assets"]["a1"]["keys"]["usdz"] == "usd/a1/a1.usdz"


def test_temp_inputs_cleaned_up(tmp_path, monkeypatch):
    captured = {}

    def _spy(textured, collision, physics, out_path, **kw):
        captured["textured"] = textured
        return _fake_compose(textured, collision, physics, out_path, **kw)

    monkeypatch.setattr(compose_run, "compose_usd", _spy)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    run_compose("r1", _settings(), store=store, usd_dir=str(tmp_path / "usd"), concurrency=1)
    # the temp input file the composer saw must be gone (TemporaryDirectory cleaned)
    assert not Path(captured["textured"]).exists()


def test_replace_on_rerun(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    usd_dir = tmp_path / "usd"
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    # drop a stale file into the asset dir; a rerun must clear it (replace)
    stale = usd_dir / "a1" / "STALE.txt"
    stale.write_text("old")
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    assert not stale.exists()
    assert (usd_dir / "a1" / "a1.usda").exists()


def test_skip_existing(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _counting(textured, collision, physics, out_path, **kw):
        calls["n"] += 1
        return _fake_compose(textured, collision, physics, out_path, **kw)

    monkeypatch.setattr(compose_run, "compose_usd", _counting)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    usd_dir = tmp_path / "usd"
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    assert calls["n"] == 1
    # second run with skip_existing must not recompose a1
    run_compose(
        "r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1, skip_existing=True
    )
    assert calls["n"] == 1


def test_empty_physics_parts_is_absent_not_failed(tmp_path, monkeypatch):
    def _raises_empty_parts(*a, **kw):
        raise ValueError("physics JSON 'parts' is empty")

    monkeypatch.setattr(compose_run, "compose_usd", _raises_empty_parts)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    ledger = run_compose(
        "r1", _settings(), store=store, usd_dir=str(tmp_path / "usd"), concurrency=1
    )
    # a1 (composable inputs) but empty physics -> absent, not failed
    assert ledger.status("a1") == "absent"
    assert ledger.stats().get("failed", 0) == 0


def test_compose_ledger_load_roundtrip(tmp_path):
    store = LocalStore(tmp_path)
    led = ComposeLedger.load_or_create(store, "runs/r1/compose-manifest.json", "r1", flush_every=1)
    led.record("a1", "done", keys={"usdz": "k"})
    reloaded = ComposeLedger.load_or_create(store, "runs/r1/compose-manifest.json", "r1")
    assert reloaded.status("a1") == "done"


def test_compose_asset_single(tmp_path, monkeypatch):
    from usd_pipeline.compose_run import compose_asset

    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    _seed_one(store, "a1", "runs/r1/a1/texture/t.glb", "runs/r1/a1/collision-preview/c.glb")
    out = compose_asset("a1", _settings(), store=store, run_id="r1", usd_dir=str(tmp_path / "usd"))
    assert out["status"] == "done"
    assert (tmp_path / "usd" / "a1" / "a1.usda").exists()
    assert store.exists("usd/a1/a1.usda")


def test_compose_asset_missing_inputs_is_absent(tmp_path, monkeypatch):
    from usd_pipeline.compose_run import compose_asset

    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    prefix = "runs/r1"
    m = Manifest.create(
        store,
        run_id="r1",
        prefix=prefix,
        bucket="local",
        results=[{"_id": "a1"}],
        source_url="x",
        reference_key=f"{prefix}/library-search.json",
        manifest_key=f"{prefix}/manifest.json",
    )
    m.flush()  # asset present but no acquired inputs
    out = compose_asset("a1", _settings(), store=store, run_id="r1", usd_dir=str(tmp_path / "usd"))
    assert out["status"] == "absent"


def test_is_safe_segment():
    assert _is_safe_segment("69fdc4a58102b48e9ae52b8d")
    assert not _is_safe_segment("../escape")
    assert not _is_safe_segment("a/b")
    assert not _is_safe_segment("a\\b")
    assert not _is_safe_segment("")


def test_textured_and_collision_never_collide_in_temp(tmp_path, monkeypatch):
    # both GLBs share the same R2 basename -> must resolve to DISTINCT temp paths
    seen = {}

    def _spy(textured, collision, physics, out_path, **kw):
        seen["distinct"] = textured != collision
        seen["tex"] = Path(textured).read_bytes()
        seen["col"] = Path(collision).read_bytes()
        return _fake_compose(textured, collision, physics, out_path, **kw)

    monkeypatch.setattr(compose_run, "compose_usd", _spy)
    store = LocalStore(tmp_path / "r2")
    _seed_one(
        store,
        "a1",
        "runs/r1/a1/texture/model.glb",  # same basename ...
        "runs/r1/a1/collision-preview/model.glb",  # ... as collision
        tex=b"TEXTURED",
        col=b"COLLISION",
    )
    run_compose("r1", _settings(), store=store, usd_dir=str(tmp_path / "usd"), concurrency=1)
    assert seen["distinct"] is True
    assert seen["tex"] == b"TEXTURED"
    assert seen["col"] == b"COLLISION"


def test_failed_recompose_preserves_prior_local_output(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    usd_dir = tmp_path / "usd"
    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    assert (usd_dir / "a1" / "a1.usda").exists()

    def _boom(*a, **kw):
        raise RuntimeError("usd authoring blew up")

    monkeypatch.setattr(compose_run, "compose_usd", _boom)
    ledger = run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    assert ledger.status("a1") == "failed"
    assert (usd_dir / "a1" / "a1.usda").exists()  # prior good output NOT destroyed


def test_generic_valueerror_is_failed_not_absent(tmp_path, monkeypatch):
    # contains the word "parts" but NOT the precise empty-physics fragments -> failed
    def _bad(*a, **kw):
        raise ValueError("unrelated error mentioning parts of speech")

    monkeypatch.setattr(compose_run, "compose_usd", _bad)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    ledger = run_compose(
        "r1", _settings(), store=store, usd_dir=str(tmp_path / "usd"), concurrency=1
    )
    assert ledger.status("a1") == "failed"


def test_unsafe_asset_id_is_failed_not_traversed(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    _seed_one(store, "../escape", "runs/r1/x/texture/t.glb", "runs/r1/x/collision-preview/c.glb")
    ledger = run_compose(
        "r1", _settings(), store=store, usd_dir=str(tmp_path / "usd"), concurrency=1
    )
    assert ledger.status("../escape") == "failed"


def test_rerun_prunes_orphan_r2_objects(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_run, "compose_usd", _fake_compose)
    store = LocalStore(tmp_path / "r2")
    _seed_run(store)
    usd_dir = tmp_path / "usd"
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    store.put_bytes("usd/a1/STALE.usdz", b"old")  # orphan from a hypothetical prior run
    run_compose("r1", _settings(), store=store, usd_dir=str(usd_dir), concurrency=1)
    assert not store.exists("usd/a1/STALE.usdz")  # pruned by delete_prefix
    assert store.exists("usd/a1/a1.usda")
