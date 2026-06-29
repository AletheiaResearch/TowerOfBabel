"""Typer command-line interface."""

from __future__ import annotations

import json

import typer

from usd_pipeline.acquire import DEFAULT_PAGE_LIMIT, run_acquisition
from usd_pipeline.config import Settings
from usd_pipeline.manifest import Manifest
from usd_pipeline.r2 import R2Store
from usd_pipeline.storage import LocalStore, Store

app = typer.Typer(help="USD/USDz pipeline — Palatial asset acquisition.", no_args_is_help=True)


def _settings(bucket: str | None) -> Settings:
    settings = Settings()
    if bucket:
        settings = settings.model_copy(update={"r2_bucket": bucket})
    return settings


def _resolve_store(settings: Settings, local: str | None) -> Store:
    if local:
        return LocalStore(local)
    store = R2Store.from_settings(settings)
    store.verify_bucket()
    return store


@app.command()
def acquire(
    limit: int | None = typer.Option(None, help="Max assets to process."),
    concurrency: int | None = typer.Option(None, help="Worker threads."),
    query: str = typer.Option("", help="Library search query."),
    page: int = typer.Option(1),
    page_limit: int = typer.Option(DEFAULT_PAGE_LIMIT, "--page-limit", help="Search page size."),
    run_id: str | None = typer.Option(None, help="Override the run id."),
    local: str | None = typer.Option(None, help="Write to a local directory instead of R2."),
    bucket: str | None = typer.Option(None, help="Override the R2 bucket."),
) -> None:
    """Run a full acquisition: search, then fetch every asset's parts."""
    settings = _settings(bucket)
    store = _resolve_store(settings, local)
    manifest = run_acquisition(
        settings,
        store=store,
        limit=limit,
        concurrency=concurrency,
        query=query,
        page=page,
        page_limit=page_limit,
        run_id=run_id,
        install_signal_handlers=True,
    )
    typer.echo(json.dumps(manifest.stats(), indent=2))


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id to resume."),
    concurrency: int | None = typer.Option(None),
    local: str | None = typer.Option(None),
    bucket: str | None = typer.Option(None),
) -> None:
    """Resume an interrupted run, skipping already-completed steps."""
    settings = _settings(bucket)
    store = _resolve_store(settings, local)
    manifest = run_acquisition(
        settings,
        store=store,
        concurrency=concurrency,
        resume_run_id=run_id,
        install_signal_handlers=True,
    )
    typer.echo(json.dumps(manifest.stats(), indent=2))


@app.command()
def compose(
    run_id: str = typer.Argument(..., help="Acquired run id to compose USD for."),
    usd_dir: str | None = typer.Option(None, help="Global local USD output dir (default ~/usd)."),
    concurrency: int | None = typer.Option(None, help="Compose worker threads."),
    limit: int | None = typer.Option(None, help="Max assets to compose."),
    skip_existing: bool = typer.Option(False, help="Skip assets already composed (resume)."),
    delight: bool = typer.Option(False, help="Clip baked highlights from the albedo."),
    inertia: str = typer.Option("auto", help="auto | predicted | derive."),
    no_flip_v: bool = typer.Option(False, "--no-flip-v", help="Don't flip texture V."),
    no_usdz: bool = typer.Option(False, "--no-usdz", help="Skip the .usdz package."),
    local: str | None = typer.Option(None, help="Read/write a local directory instead of R2."),
    bucket: str | None = typer.Option(None, help="Override the R2 bucket."),
) -> None:
    """Compose USD/USDz from an acquired run's parts (textured + collision + physics)."""
    from usd_pipeline.compose_run import run_compose  # lazy: heavy USD stack

    settings = _settings(bucket)
    store = _resolve_store(settings, local)
    ledger = run_compose(
        run_id,
        settings,
        store=store,
        usd_dir=usd_dir,
        concurrency=concurrency,
        limit=limit,
        skip_existing=skip_existing,
        inertia=inertia,
        delight=delight,
        flip_v=not no_flip_v,
        usdz=not no_usdz,
    )
    typer.echo(json.dumps(ledger.stats(), indent=2))


@app.command()
def status(
    run_id: str = typer.Argument(..., help="Run id to inspect."),
    local: str | None = typer.Option(None),
    bucket: str | None = typer.Option(None),
) -> None:
    """Print the manifest stats for a run."""
    settings = _settings(bucket)
    store = _resolve_store(settings, local)
    manifest = Manifest.load(store, f"runs/{run_id}/manifest.json")
    if manifest is None:
        typer.echo(f"No manifest found for run '{run_id}'.", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(manifest.stats(), indent=2))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
