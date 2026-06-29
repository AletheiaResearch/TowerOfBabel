"""Batch USD composition over an acquired run.

For each acquired asset in ``runs/<run-id>/``: resolve the three compose inputs from the
acquisition manifest (so filenames are never guessed), download them to a temp dir, run
``compose_usd`` into a staging dir, then on success replace the global local USD dir
``<usd_dir>/<assetId>/`` and the global R2 prefix ``usd/<assetId>/`` (parallel to
``runs/`` — a single canonical USD library, keyed by asset id, pruned + rewritten on
rerun), and drop the temp inputs. Progress is tracked in
``runs/<run-id>/compose-manifest.json`` so a run can skip already-composed assets.
"""

from __future__ import annotations

import datetime
import json
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from usd_pipeline.compose import compose_usd
from usd_pipeline.config import Settings
from usd_pipeline.manifest import Manifest
from usd_pipeline.r2 import R2Store
from usd_pipeline.storage import Store

# Composed USD lives under a global `usd/<assetId>/` prefix (parallel to `runs/`), keyed by
# asset id and replaced on rerun — i.e. a single canonical USD library, not per-run.
USD_PREFIX = "usd"

# compose_usd raises ValueError with one of these when there is no rigid body to author;
# that is an expected data gap (-> absent), not a failure. Matched precisely (not just
# "parts") so unrelated ValueErrors stay classified as real failures.
_EMPTY_PHYSICS_FRAGMENTS = ("no 'parts' array", "'parts' is empty")


def _is_safe_segment(asset_id: str) -> bool:
    """True if asset_id is safe as a single path/key segment (no traversal/separators)."""
    return bool(asset_id) and not ({"/", "\\"} & set(asset_id)) and ".." not in asset_id


_CONTENT_TYPES = {
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usda",
    ".usdc": "model/vnd.usdc",
    ".usdz": "model/vnd.usdz+zip",
    ".png": "image/png",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _content_type(path: str) -> str | None:
    return _CONTENT_TYPES.get(Path(path).suffix.lower())


class ComposeLedger:
    """Thread-safe per-asset compose progress, persisted via a Store."""

    def __init__(self, store: Store, key: str, data: dict, flush_every: int = 8):
        self._store = store
        self._key = key
        self._data = data
        self._lock = threading.RLock()
        self._flush_every = max(1, flush_every)
        self._dirty = 0

    @classmethod
    def load_or_create(
        cls, store: Store, key: str, run_id: str, flush_every: int = 8
    ) -> ComposeLedger:
        data = store.get_json(key)
        if not isinstance(data, dict):
            data = {"run_id": run_id, "created_at": _now_iso(), "assets": {}, "stats": {}}
        return cls(store, key, data, flush_every)

    def status(self, asset_id: str) -> str | None:
        with self._lock:
            entry = self._data["assets"].get(asset_id)
            return entry.get("status") if entry else None

    def record(
        self,
        asset_id: str,
        status: str,
        *,
        keys: dict | None = None,
        local: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = None
        with self._lock:
            self._data["assets"][asset_id] = {
                "status": status,
                "keys": keys or {},
                "local": local,
                "error": error,
                "completed_at": _now_iso(),
            }
            self._data["updated_at"] = _now_iso()
            self._dirty += 1
            if self._dirty >= self._flush_every:
                payload = self._serialize_locked()
                self._dirty = 0
        if payload is not None:
            self._store.put_bytes(self._key, payload, "application/json")

    def flush(self) -> None:
        with self._lock:
            payload = self._serialize_locked()
            self._dirty = 0
        self._store.put_bytes(self._key, payload, "application/json")

    def _serialize_locked(self) -> bytes:
        self._data["updated_at"] = _now_iso()
        self._data["stats"] = self._compute_stats()
        return json.dumps(self._data, indent=2, ensure_ascii=False).encode("utf-8")

    def stats(self) -> dict:
        with self._lock:
            return self._compute_stats()

    def _compute_stats(self) -> dict:
        counts: dict[str, int] = {}
        for entry in self._data["assets"].values():
            status = entry.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        return {"total": len(self._data["assets"]), **counts}

    @property
    def data(self) -> dict:
        with self._lock:
            return self._data


def _resolve_inputs(asset: dict) -> tuple[str, str, str] | None:
    """Return (textured_key, collision_key, physics_key) or None if any is missing."""
    steps = asset.get("steps", {})
    textured = steps.get("texture", {}).get("keys", {}).get("artifact")
    collision = steps.get("collision-preview", {}).get("keys", {}).get("artifact")
    physics = steps.get("embedded-physics", {}).get("keys", {}).get("file")
    if textured and collision and physics:
        return textured, collision, physics
    return None


def _compose_one(
    store: Store,
    usd_dir: Path,
    asset_id: str,
    textured_key: str,
    collision_key: str,
    physics_key: str,
    *,
    inertia: str,
    delight: bool,
    flip_v: bool,
    usdz: bool,
) -> dict:
    if not _is_safe_segment(asset_id):
        raise ValueError(f"unsafe asset id for a path/key segment: {asset_id!r}")
    asset_dir = usd_dir / asset_id

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Name inputs by ROLE (preserving the suffix trimesh needs) so the textured and
        # collision GLBs can never collide on a shared upstream basename.
        textured = tmp_path / f"textured{Path(textured_key).suffix}"
        collision = tmp_path / f"collision{Path(collision_key).suffix}"
        physics = tmp_path / "physics.json"
        store.download_to(textured_key, textured)
        store.download_to(collision_key, collision)
        store.download_to(physics_key, physics)

        # Compose into a staging dir and only swap into asset_dir on success, so a failed
        # rerun never destroys the previously-good local output.
        staging = tmp_path / "out"
        staging.mkdir()
        result = compose_usd(
            str(textured),
            str(collision),
            str(physics),
            str(staging / f"{asset_id}.usda"),
            inertia=inertia,
            delight=delight,
            flip_v=flip_v,
            usdz=usdz,
            verbose=False,
        )
        if asset_dir.exists():
            shutil.rmtree(asset_dir)
        asset_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(asset_dir))  # local replace; temp inputs cleaned on exit

    # True R2 replace: prune the prefix, then upload the current outputs (no orphans).
    store.delete_prefix(f"{USD_PREFIX}/{asset_id}/")
    keys: dict[str, str] = {}
    for kind, produced in result.items():
        if not produced:
            continue
        final = asset_dir / Path(produced).name
        if final.exists():
            key = f"{USD_PREFIX}/{asset_id}/{final.name}"
            store.put_bytes(key, final.read_bytes(), _content_type(str(final)))
            keys[kind] = key
    return {"status": "done", "keys": keys, "local": str(asset_dir)}


def run_compose(
    run_id: str,
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    usd_dir: str | None = None,
    concurrency: int | None = None,
    limit: int | None = None,
    skip_existing: bool = False,
    inertia: str = "auto",
    delight: bool = False,
    flip_v: bool = True,
    usdz: bool = True,
) -> ComposeLedger:
    """Compose USD/USDz for every composable asset in an acquired run."""
    settings = settings or Settings()
    if store is None:
        store = R2Store.from_settings(settings)
        store.verify_bucket()
    usd_dir_path = Path(usd_dir or settings.pipeline_usd_dir).expanduser()
    usd_dir_path.mkdir(parents=True, exist_ok=True)
    concurrency = concurrency or settings.compose_concurrency
    prefix = f"runs/{run_id}"

    manifest = Manifest.load(store, f"{prefix}/manifest.json")
    if manifest is None:
        raise FileNotFoundError(f"no acquisition manifest at {prefix}/manifest.json")
    ledger = ComposeLedger.load_or_create(
        store, f"{prefix}/compose-manifest.json", run_id, settings.manifest_flush_every
    )

    targets: list[tuple[str, str, str, str]] = []
    for asset_id, asset in manifest.data["assets"].items():
        inputs = _resolve_inputs(asset)
        if inputs is None:
            ledger.record(asset_id, "absent", error="missing inputs (texture/collision/physics)")
            continue
        if skip_existing and ledger.status(asset_id) == "done":
            continue
        targets.append((asset_id, *inputs))
    if limit is not None:
        targets = targets[:limit]

    try:
        with (
            Progress(console=Console(stderr=True)) as progress,
            ThreadPoolExecutor(max_workers=concurrency) as ex,
        ):
            task = progress.add_task("Composing USD", total=len(targets))
            futures = {
                ex.submit(
                    _compose_one,
                    store,
                    usd_dir_path,
                    asset_id,
                    textured,
                    collision,
                    physics,
                    inertia=inertia,
                    delight=delight,
                    flip_v=flip_v,
                    usdz=usdz,
                ): asset_id
                for (asset_id, textured, collision, physics) in targets
            }
            for fut in as_completed(futures):
                asset_id = futures[fut]
                exc = fut.exception()
                if exc is None:
                    ledger.record(asset_id, **fut.result())
                elif isinstance(exc, ValueError) and any(
                    frag in str(exc) for frag in _EMPTY_PHYSICS_FRAGMENTS
                ):
                    # empty/missing physics parts: nothing to author -> not composable,
                    # an expected data gap rather than a failure.
                    ledger.record(asset_id, "absent", error=str(exc))
                else:
                    ledger.record(asset_id, "failed", error=repr(exc))
                    progress.console.log(f"compose {asset_id} failed: {exc!r}")
                progress.advance(task)
    finally:
        ledger.flush()

    return ledger
