"""Storage abstraction: a small Store protocol with a filesystem implementation."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int: ...
    def put_stream(
        self, key: str, chunks: Iterable[bytes], content_type: str | None = None
    ) -> int: ...
    def put_json(self, key: str, obj: object) -> int: ...
    def get_json(self, key: str) -> object | None: ...
    def download_to(self, key: str, dest: str | Path) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def exists(self, key: str) -> bool: ...


class LocalStore:
    """Filesystem-backed Store. Keys map to paths under `root`."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int:
        self._path(key).write_bytes(data)
        return len(data)

    def put_stream(self, key: str, chunks: Iterable[bytes], content_type: str | None = None) -> int:
        total = 0
        with self._path(key).open("wb") as f:
            for chunk in chunks:
                f.write(chunk)
                total += len(chunk)
        return total

    def put_json(self, key: str, obj: object) -> int:
        data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        return self.put_bytes(key, data, "application/json")

    def get_json(self, key: str) -> object | None:
        p = self.root / key
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def download_to(self, key: str, dest: str | Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.root / key, dest)

    def delete_prefix(self, prefix: str) -> int:
        base = self.root / prefix
        if base.is_dir():
            count = sum(1 for p in base.rglob("*") if p.is_file())
            shutil.rmtree(base)
            return count
        return 0

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()
