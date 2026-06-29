# Tower of Babel

A Python pipeline that turns the [Palatial](https://palatial.cloud) asset library into
Isaac-Sim-ready **USD / USDz** files, stored in Cloudflare R2.

Two stages:

- **Acquire** — pull every library asset's parts (meshes, textures, physics, validation
  artifacts) into R2 under `runs/<run-id>/`, tracked by a resumable manifest.
- **Compose** — assemble each asset's textured mesh + convex-hull collider + physics spec
  into a rigid-body `.usda` / `.usdz`, written to a global `usd/<assetId>/` library.

It's a [`uv`](https://docs.astral.sh/uv/) project exposing a library and the `usd-pipeline`
CLI (`acquire` · `resume` · `status` · `compose`).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in your Cloudflare R2 endpoint, bucket, and keys
```

## CLI

```bash
# Full acquisition into R2 (endpoint + bucket from .env)
uv run usd-pipeline acquire

# Smoke test: only the first asset, into a local directory (no R2 needed)
uv run usd-pipeline acquire --limit 1 --local ./output

# Resume an interrupted run (skips completed steps)
uv run usd-pipeline resume 2026-06-25T14-30-00Z

# Inspect progress
uv run usd-pipeline status 2026-06-25T14-30-00Z

# Compose USD/USDz from an acquired run's parts (textured + collision + physics)
uv run usd-pipeline compose 2026-06-25T14-30-00Z --delight
```

## Compose (USD/USDz)

`compose` turns each acquired asset's parts into an Isaac-Sim-ready rigid-body USD:

- inputs (resolved from the acquisition manifest): the textured mesh GLB, the
  collision-hull GLB, and `embedded-physics.json` (the Palatial "newton" spec);
- outputs: `<assetId>.usda` + `.usdz` + `_basecolor.png`, written to a global local USD dir
  (`PIPELINE_USD_DIR`, default `~/usd`; `--usd-dir` to override) as `<usdDir>/<assetId>/`,
  and **also uploaded to R2 under a global `usd/<assetId>/` prefix** (parallel to `runs/`,
  keyed by asset id — a single canonical USD library, not per-run);
- each asset's USD dir is replaced on rerun; progress is tracked in
  `runs/<run-id>/compose-manifest.json` (`--skip-existing` resumes, default replaces);
- assets missing **any** of the textured mesh, collision mesh, or embedded-physics JSON —
  or whose physics has no `parts` — are recorded `absent` (not composable).

```bash
uv run usd-pipeline compose <run-id> [--usd-dir ~/usd] [--concurrency 4] [--limit N] \
    [--skip-existing] [--delight] [--inertia auto|predicted|derive] [--no-usdz] [--local DIR]
```

## Library use (notebooks)

```python
from usd_pipeline import run_acquisition, run_compose, compose_usd, Settings, LocalStore

manifest = run_acquisition(Settings(), store=LocalStore("./output"), limit=5)
print(manifest.stats())

# Compose the acquired run (USD/USDz) ...
ledger = run_compose(manifest.data["run_id"], Settings(), store=LocalStore("./output"),
                     usd_dir="./usd", limit=5)
print(ledger.stats())

# ... or compose a single asset directly from local files:
compose_usd("textured.glb", "collision.glb", "physics.json", "out/asset.usda", usdz=True)
```

## Notebook

An interactive [marimo](https://marimo.io) notebook drives the whole flow — browse the
library, pick one asset, acquire just that asset, compose its USD/USDz, and preview it
(textured GLB via `<model-viewer>`, plus a best-effort three.js render of the actual
`.usdz`, and a download/AR link). It runs entirely against the public Palatial API into a
local temp dir — **no R2 credentials needed**.

```bash
uv run --extra notebook marimo edit notebooks/explore.py
```

## How it works

For every result of the library search, the tool runs 8 steps and stores both each
endpoint's JSON response and the file its `downloadUrl` points to:

1. `detail` — `GET /assets/{id}` (unlocked by minting a per-asset `asset_share_viewer`
   cookie via `POST /share/library-viewer-session`)
2-6. `process-file/*` — `shape-generation`, `texture`, `collision-preview`,
   `physics-predictions`, `validation-playback`
7. `embedded-physics` — `GET /assets/{id}/embedded/physics` (inline JSON)
8. `validation-report` — `GET /assets/{id}/media/validation-report`

Assets are fetched concurrently (`--concurrency`, default 8). A process-file response with
no `downloadUrl`, or a validation-report `404` (no report exists), is recorded as `absent`
(not an error). An asset that is gated server-side (`403` "not available in the library")
has its steps recorded as `skipped` (discarded, not a failure). A shared HDR environment
map is fetched once per run and saved at the run root.

## Layout in R2

```
runs/<run-id>/
  library-search.json                    # search reference
  studio_country_hall_1k-3e5b7ed8.hdr    # shared HDR env map (once per run)
  manifest.json                          # acquisition progress / recovery
  compose-manifest.json                  # compose progress / recovery
  <assetId>/
    detail.json
    <kind>.response.json + <kind>/<file>     # shape-generation, texture, ...
    embedded-physics.json
    validation-report.response.json + validation-report/<file>

usd/                                     # global composed USD library (parallel to runs/)
  <assetId>/
    <assetId>.usda
    <assetId>.usdz
    <assetId>_basecolor.png
```

Each run uses a UTC-timestamped prefix; the manifest records per-step status (`done`,
`absent`, `failed`, `skipped`) so runs are resumable: `resume <run-id>` reruns only the
steps that are not yet complete.

## Configuration

All settings come from environment variables / `.env` (see `.env.example`):
`R2_ENDPOINT_URL`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_REGION`,
`PALATIAL_BASE_URL`, `PALATIAL_COOKIE` (optional override), `PIPELINE_CONCURRENCY`.
Credentials are never committed.

## Tests

```bash
uv run pytest            # unit tests (HTTP mocked with respx, R2 mocked with moto)
uv run pytest -m live    # opt-in checks against the real Palatial API (network)
```

## License

Copyright © 2026 Nejc Drobnič.

Licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later) — see
[`LICENSE`](LICENSE).
