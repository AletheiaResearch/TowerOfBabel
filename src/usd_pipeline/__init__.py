"""USD/USDz pipeline — acquisition stage."""

from typing import TYPE_CHECKING

from usd_pipeline.acquire import resume, run_acquisition
from usd_pipeline.config import Settings
from usd_pipeline.manifest import Manifest
from usd_pipeline.palatial import PalatialClient
from usd_pipeline.r2 import R2Store
from usd_pipeline.storage import LocalStore

if TYPE_CHECKING:
    from usd_pipeline.compose import compose_usd
    from usd_pipeline.compose_run import run_compose

__version__ = "0.1.0"

__all__ = [
    "Settings",
    "run_acquisition",
    "resume",
    "run_compose",
    "compose_usd",
    "Manifest",
    "LocalStore",
    "R2Store",
    "PalatialClient",
    "__version__",
]


def __getattr__(name: str):
    # Lazy: composition pulls in the heavy USD stack (pxr/trimesh/scipy); acquisition-only
    # use should not pay that import cost.
    if name == "compose_usd":
        from usd_pipeline.compose import compose_usd

        return compose_usd
    if name == "run_compose":
        from usd_pipeline.compose_run import run_compose

        return run_compose
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
