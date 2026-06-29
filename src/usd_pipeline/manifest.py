"""Thread-safe acquisition progress ledger, persisted via a Store."""

from __future__ import annotations

import datetime
import json
import threading

from usd_pipeline.models import STEP_NAMES, StepOutcome
from usd_pipeline.storage import Store

# Steps that successfully acquired their data (count toward "done").
_ACQUIRED_STATES = {"done", "absent"}
# Steps that are terminal and must not be retried on resume (acquired or deliberately discarded).
_TERMINAL_STATES = {"done", "absent", "skipped"}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class Manifest:
    def __init__(self, store: Store, manifest_key: str, data: dict, flush_every: int = 25):
        self._store = store
        self._manifest_key = manifest_key
        self._data = data
        self._lock = threading.RLock()
        self._flush_every = max(1, flush_every)
        self._dirty = 0

    @classmethod
    def create(
        cls,
        store: Store,
        *,
        run_id: str,
        prefix: str,
        bucket: str,
        results: list[dict],
        source_url: str,
        reference_key: str,
        manifest_key: str,
        flush_every: int = 25,
    ) -> Manifest:
        assets: dict[str, dict] = {}
        for r in results:
            aid = r.get("_id") or r.get("id")
            if not aid:
                continue
            assets[aid] = {"asset_name": r.get("asset_name") or r.get("name"), "steps": {}}
        data = {
            "run_id": run_id,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "bucket": bucket,
            "prefix": prefix,
            "source": {"url": source_url, "result_count": len(assets)},
            "reference_key": reference_key,
            "assets": assets,
            "stats": {},
        }
        m = cls(store, manifest_key, data, flush_every)
        with m._lock:
            m._data["stats"] = m._compute_stats()
        return m

    @classmethod
    def load(cls, store: Store, manifest_key: str, flush_every: int = 25) -> Manifest | None:
        data = store.get_json(manifest_key)
        if data is None:
            return None
        return cls(store, manifest_key, data, flush_every)

    def asset_ids(self) -> list[str]:
        with self._lock:
            return list(self._data["assets"].keys())

    def should_run(self, asset_id: str, step_name: str) -> bool:
        with self._lock:
            step = self._data["assets"].get(asset_id, {}).get("steps", {}).get(step_name)
            if step is None:
                return True
            return step.get("status") not in _TERMINAL_STATES

    def update_step(self, asset_id: str, step_name: str, outcome: StepOutcome) -> None:
        payload = None
        with self._lock:
            record = outcome.to_dict()
            if record.get("completed_at") is None:
                record["completed_at"] = _now_iso()
            asset = self._data["assets"].setdefault(asset_id, {"asset_name": None, "steps": {}})
            asset["steps"][step_name] = record
            self._data["updated_at"] = _now_iso()
            self._dirty += 1
            if self._dirty >= self._flush_every:
                payload = self._serialize_locked()
                self._dirty = 0
        # Upload outside the lock so a network round-trip never stalls the workers.
        if payload is not None:
            self._store.put_bytes(self._manifest_key, payload, "application/json")

    def flush(self) -> None:
        with self._lock:
            payload = self._serialize_locked()
            self._dirty = 0
        self._store.put_bytes(self._manifest_key, payload, "application/json")

    def _serialize_locked(self) -> bytes:
        """Refresh stats + timestamp and serialize a consistent snapshot (caller holds lock)."""
        self._data["updated_at"] = _now_iso()
        self._data["stats"] = self._compute_stats()
        return json.dumps(self._data, indent=2, ensure_ascii=False).encode("utf-8")

    def stats(self) -> dict:
        with self._lock:
            return self._compute_stats()

    @property
    def data(self) -> dict:
        with self._lock:
            return self._data

    def _compute_stats(self) -> dict:
        steps_done = steps_failed = steps_skipped = assets_done = 0
        for asset in self._data["assets"].values():
            steps = asset.get("steps", {})
            done_here = sum(1 for s in steps.values() if s.get("status") in _ACQUIRED_STATES)
            steps_done += done_here
            steps_failed += sum(1 for s in steps.values() if s.get("status") == "failed")
            steps_skipped += sum(1 for s in steps.values() if s.get("status") == "skipped")
            if done_here == len(STEP_NAMES):
                assets_done += 1
        return {
            "assets_total": len(self._data["assets"]),
            "assets_done": assets_done,
            "steps_done": steps_done,
            "steps_failed": steps_failed,
            "steps_skipped": steps_skipped,
        }
