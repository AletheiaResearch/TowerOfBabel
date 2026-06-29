from usd_pipeline.manifest import Manifest
from usd_pipeline.models import StepOutcome, StepStatus
from usd_pipeline.storage import LocalStore

RESULTS = [
    {"_id": "a1", "asset_name": "Alpha"},
    {"_id": "a2", "name": "Beta"},
]
MK = "runs/r1/manifest.json"


def _create(tmp_path, flush_every=25):
    store = LocalStore(tmp_path)
    m = Manifest.create(
        store,
        run_id="r1",
        prefix="runs/r1",
        bucket="b",
        results=RESULTS,
        source_url="http://s",
        reference_key="runs/r1/library-search.json",
        manifest_key=MK,
        flush_every=flush_every,
    )
    return store, m


def test_create_lists_assets(tmp_path):
    _store, m = _create(tmp_path)
    assert set(m.asset_ids()) == {"a1", "a2"}
    assert m.data["assets"]["a1"]["asset_name"] == "Alpha"
    assert m.data["source"]["result_count"] == 2


def test_should_run_then_done_skips(tmp_path):
    _store, m = _create(tmp_path)
    assert m.should_run("a1", "detail") is True
    m.update_step("a1", "detail", StepOutcome(status=StepStatus.DONE, keys={"file": "k"}))
    assert m.should_run("a1", "detail") is False


def test_absent_also_skips_but_failed_reruns(tmp_path):
    _store, m = _create(tmp_path)
    m.update_step("a1", "texture", StepOutcome(status=StepStatus.ABSENT))
    m.update_step("a1", "shape-generation", StepOutcome(status=StepStatus.FAILED, error="x"))
    assert m.should_run("a1", "texture") is False
    assert m.should_run("a1", "shape-generation") is True


def test_throttled_flush_then_load_roundtrip(tmp_path):
    store, m = _create(tmp_path, flush_every=1)
    m.update_step("a1", "detail", StepOutcome(status=StepStatus.DONE, keys={"file": "k"}))
    loaded = Manifest.load(store, MK)
    assert loaded is not None
    assert loaded.should_run("a1", "detail") is False


def test_load_missing_returns_none(tmp_path):
    store = LocalStore(tmp_path)
    assert Manifest.load(store, "runs/nope/manifest.json") is None


def test_stats_counts(tmp_path):
    _store, m = _create(tmp_path)
    m.update_step("a1", "detail", StepOutcome(status=StepStatus.DONE))
    m.update_step("a2", "detail", StepOutcome(status=StepStatus.FAILED, error="x"))
    s = m.stats()
    assert s["assets_total"] == 2
    assert s["steps_done"] == 1
    assert s["steps_failed"] == 1


def test_skipped_is_terminal_and_counted(tmp_path):
    _store, m = _create(tmp_path)
    m.update_step("a1", "detail", StepOutcome(status=StepStatus.SKIPPED, error="forbidden"))
    # skipped is terminal: a resume must not retry it
    assert m.should_run("a1", "detail") is False
    s = m.stats()
    assert s["steps_skipped"] == 1
    assert s["steps_failed"] == 0
    assert s["steps_done"] == 0
