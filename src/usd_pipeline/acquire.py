"""Acquisition orchestration: search → per-asset steps → manifest, resumable."""

from __future__ import annotations

import datetime
import signal
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

from rich.console import Console
from rich.progress import Progress

from usd_pipeline.config import Settings
from usd_pipeline.manifest import Manifest
from usd_pipeline.models import PROCESS_FILE_KINDS, StepOutcome, StepStatus
from usd_pipeline.palatial import PalatialClient, PalatialError, PalatialHTTPError
from usd_pipeline.r2 import R2Store
from usd_pipeline.storage import Store

DEFAULT_PAGE_LIMIT = 812

# A shared HDR environment map fetched once per run (not per asset).
RUN_HDR_URL = "https://dashboard.palatial.cloud/assets/studio_country_hall_1k-3e5b7ed8.hdr"


def _classify_http_error(e: PalatialHTTPError) -> StepStatus | None:
    """Map a Palatial HTTP error to a soft (non-failure) status, or None to keep it a failure.

    - 404 (not found): the resource simply doesn't exist (e.g. no validation report) -> ABSENT
    - 403 forbidden / "not available in the library": the asset is gated -> SKIPPED (discarded)
    """
    if e.status_code == 404:
        return StepStatus.ABSENT
    if e.status_code == 403 and (
        "not available" in str(e).lower() or "forbidden" in str(e).lower()
    ):
        return StepStatus.SKIPPED
    return None


def make_run_id() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def filename_from_url(url: str, default: str = "artifact.bin") -> str:
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name or default


def _key(prefix: str, asset_id: str, *parts: str) -> str:
    return "/".join([prefix, asset_id, *parts])


def _verify_length(resp, written: int) -> None:
    """Raise if the body was truncated vs its declared Content-Length.

    Skipped when the response is content-encoded (the decoded size legitimately
    differs from the on-the-wire Content-Length).
    """
    if resp.headers.get("content-encoding"):
        return
    declared = resp.headers.get("content-length")
    if not declared:
        return
    try:
        expected = int(declared)
    except ValueError:
        return
    if expected != written:
        raise PalatialError(f"truncated download: wrote {written} of {expected} bytes")


# --- per-step workers (each returns a StepOutcome; never raises) -----------


def _do_detail(client: PalatialClient, store: Store, prefix: str, asset_id: str) -> StepOutcome:
    try:
        cookie = None if client.cookie else client.mint_viewer_cookie(asset_id)
        detail = client.asset_detail(asset_id, cookie=cookie)
        key = _key(prefix, asset_id, "detail.json")
        n = store.put_json(key, detail)
        return StepOutcome(StepStatus.DONE, keys={"file": key}, http_status=200, bytes=n)
    except PalatialHTTPError as e:
        soft = _classify_http_error(e)
        return StepOutcome(soft or StepStatus.FAILED, http_status=e.status_code, error=str(e))
    except Exception as e:
        return StepOutcome(StepStatus.FAILED, error=str(e))


def _do_process_file(
    client: PalatialClient, store: Store, prefix: str, asset_id: str, kind: str
) -> StepOutcome:
    try:
        resp = client.process_file(asset_id, kind)
        resp_key = _key(prefix, asset_id, f"{kind}.response.json")
        store.put_json(resp_key, resp)
        url = resp.get("downloadUrl")
        state = resp.get("state")
        if not url:
            return StepOutcome(
                StepStatus.ABSENT, keys={"response": resp_key}, state=state, http_status=200
            )
        art_key = _key(prefix, asset_id, kind, filename_from_url(url, f"{kind}.bin"))
        with client.download(url) as dl:
            n = store.put_stream(art_key, dl.iter_bytes(), dl.headers.get("content-type"))
            _verify_length(dl, n)
        return StepOutcome(
            StepStatus.DONE,
            keys={"response": resp_key, "artifact": art_key},
            state=state,
            http_status=200,
            bytes=n,
        )
    except PalatialHTTPError as e:
        soft = _classify_http_error(e)
        return StepOutcome(soft or StepStatus.FAILED, http_status=e.status_code, error=str(e))
    except Exception as e:
        return StepOutcome(StepStatus.FAILED, error=str(e))


def _do_embedded_physics(
    client: PalatialClient, store: Store, prefix: str, asset_id: str
) -> StepOutcome:
    try:
        data = client.embedded_physics(asset_id)
        key = _key(prefix, asset_id, "embedded-physics.json")
        n = store.put_json(key, data)
        return StepOutcome(StepStatus.DONE, keys={"file": key}, http_status=200, bytes=n)
    except PalatialHTTPError as e:
        soft = _classify_http_error(e)
        return StepOutcome(soft or StepStatus.FAILED, http_status=e.status_code, error=str(e))
    except Exception as e:
        return StepOutcome(StepStatus.FAILED, error=str(e))


def _do_validation_report(
    client: PalatialClient, store: Store, prefix: str, asset_id: str
) -> StepOutcome:
    try:
        resp = client.media_validation_report(asset_id)
        resp_key = _key(prefix, asset_id, "validation-report.response.json")
        store.put_json(resp_key, resp)
        url = resp.get("downloadUrl")
        if not url:
            return StepOutcome(StepStatus.ABSENT, keys={"response": resp_key}, http_status=200)
        art_key = _key(prefix, asset_id, "validation-report", filename_from_url(url, "report.bin"))
        with client.download(url) as dl:
            n = store.put_stream(art_key, dl.iter_bytes(), dl.headers.get("content-type"))
            _verify_length(dl, n)
        return StepOutcome(
            StepStatus.DONE,
            keys={"response": resp_key, "artifact": art_key},
            http_status=200,
            bytes=n,
        )
    except PalatialHTTPError as e:
        soft = _classify_http_error(e)
        return StepOutcome(soft or StepStatus.FAILED, http_status=e.status_code, error=str(e))
    except Exception as e:
        return StepOutcome(StepStatus.FAILED, error=str(e))


def _process_asset(
    client_factory: Callable[[], PalatialClient],
    store: Store,
    manifest: Manifest,
    prefix: str,
    asset_id: str,
) -> None:
    with client_factory() as client:
        if manifest.should_run(asset_id, "detail"):
            manifest.update_step(asset_id, "detail", _do_detail(client, store, prefix, asset_id))
        for kind in PROCESS_FILE_KINDS:
            if manifest.should_run(asset_id, kind):
                manifest.update_step(
                    asset_id, kind, _do_process_file(client, store, prefix, asset_id, kind)
                )
        if manifest.should_run(asset_id, "embedded-physics"):
            manifest.update_step(
                asset_id, "embedded-physics", _do_embedded_physics(client, store, prefix, asset_id)
            )
        if manifest.should_run(asset_id, "validation-report"):
            manifest.update_step(
                asset_id,
                "validation-report",
                _do_validation_report(client, store, prefix, asset_id),
            )


def _save_run_hdr(client: PalatialClient, store: Store, prefix: str) -> str:
    """Fetch the shared HDR environment map once per run. Idempotent (skips if present)."""
    key = f"{prefix}/{filename_from_url(RUN_HDR_URL)}"
    if store.exists(key):
        return key
    with client.download(RUN_HDR_URL) as dl:
        store.put_stream(key, dl.iter_bytes(), dl.headers.get("content-type"))
    return key


def _install_flush_handler(manifest: Manifest) -> dict:
    """Install SIGINT/SIGTERM handlers that flush the manifest. Returns prior handlers."""

    def handler(signum, frame):
        manifest.flush()
        raise KeyboardInterrupt

    previous: dict = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not in main thread (e.g. tests) — rely on try/finally flush
    return previous


def _restore_handlers(previous: dict) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def run_acquisition(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    limit: int | None = None,
    concurrency: int | None = None,
    query: str = "",
    page: int = 1,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    run_id: str | None = None,
    resume_run_id: str | None = None,
    install_signal_handlers: bool = False,
) -> Manifest:
    settings = settings or Settings()
    if store is None:
        store = R2Store.from_settings(settings)
        store.verify_bucket()
    concurrency = concurrency or settings.pipeline_concurrency
    run_id = resume_run_id or run_id or make_run_id()
    prefix = f"runs/{run_id}"
    manifest_key = f"{prefix}/manifest.json"
    reference_key = f"{prefix}/library-search.json"
    bucket = getattr(store, "bucket", "local")

    def client_factory() -> PalatialClient:
        return PalatialClient(
            base_url=settings.palatial_base_url,
            cookie=settings.palatial_cookie,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
        )

    if resume_run_id:
        manifest = Manifest.load(store, manifest_key, flush_every=settings.manifest_flush_every)
        if manifest is None:
            raise FileNotFoundError(f"no manifest to resume at {manifest_key}")
    else:
        with client_factory() as client:
            search = client.search(q=query, page=page, limit=page_limit)
        store.put_json(reference_key, search)
        results = search.get("results", [])
        if limit is not None:
            results = results[:limit]
        source_url = (
            f"{settings.palatial_base_url}/library/search?q={query}&page={page}&limit={page_limit}"
        )
        manifest = Manifest.create(
            store,
            run_id=run_id,
            prefix=prefix,
            bucket=bucket,
            results=results,
            source_url=source_url,
            reference_key=reference_key,
            manifest_key=manifest_key,
            flush_every=settings.manifest_flush_every,
        )
        manifest.flush()

    asset_ids = manifest.asset_ids()

    # Run-level shared asset (HDR environment map), fetched once. Supplementary: never fatal.
    try:
        with client_factory() as hdr_client:
            _save_run_hdr(hdr_client, store, prefix)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: run HDR fetch failed: {exc!r}", file=sys.stderr)

    previous_handlers = _install_flush_handler(manifest) if install_signal_handlers else None

    try:
        with (
            Progress(console=Console(stderr=True)) as progress,
            ThreadPoolExecutor(max_workers=concurrency) as ex,
        ):
            task = progress.add_task("Acquiring assets", total=len(asset_ids))
            futures = {
                ex.submit(_process_asset, client_factory, store, manifest, prefix, aid): aid
                for aid in asset_ids
            }
            for fut in as_completed(futures):
                exc = fut.exception()
                if exc is not None:
                    progress.console.log(f"asset {futures[fut]} raised: {exc!r}")
                progress.advance(task)
    finally:
        manifest.flush()
        if previous_handlers is not None:
            _restore_handlers(previous_handlers)

    return manifest


def resume(
    run_id: str,
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    concurrency: int | None = None,
    install_signal_handlers: bool = False,
) -> Manifest:
    return run_acquisition(
        settings,
        store=store,
        concurrency=concurrency,
        resume_run_id=run_id,
        install_signal_handlers=install_signal_handlers,
    )
