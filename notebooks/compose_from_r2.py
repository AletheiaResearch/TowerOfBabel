"""Compose + preview an asset from an already-acquired R2 run.

Run it with:   uv run --extra notebook marimo edit notebooks/compose_from_r2.py

Unlike explore.py (which acquires locally with no credentials), this reads runs already
in Cloudflare R2: pick a run → pick a composable asset → compose its USD/USDz (uploaded to
the global usd/<assetId>/ prefix, with a local copy for preview) → preview it. Requires R2
credentials in .env.
"""

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md(
        """
        # Compose from an R2 run

        Pick an already-acquired run in Cloudflare R2, choose a composable asset, compose
        its USD/USDz, and preview it. (Requires R2 credentials in `.env`.)
        """
    )
    return


@app.cell
def _():
    import tempfile
    from pathlib import Path

    from usd_pipeline import (
        Manifest,
        Settings,
        compose_asset,
        model_viewer_html,
        usdz_viewer_html,
    )
    from usd_pipeline.r2 import R2Store

    settings = Settings()  # reads .env for R2 endpoint/bucket/keys
    try:
        store = R2Store.from_settings(settings)
        store.verify_bucket()
        r2_error = None
    except Exception as exc:  # noqa: BLE001 - surfaced as a friendly notebook message
        store, r2_error = None, str(exc)
    usd_dir = Path(tempfile.gettempdir()) / "usd-pipeline-notebook-r2"
    return (
        Manifest,
        Path,
        compose_asset,
        model_viewer_html,
        r2_error,
        settings,
        store,
        usd_dir,
        usdz_viewer_html,
    )


@app.cell
def _(mo, r2_error, store):
    mo.stop(
        store is None,
        mo.md(f"**R2 is not configured.** Set `R2_*` in `.env`.\n\n```\n{r2_error}\n```").callout(
            kind="danger"
        ),
    )
    runs = sorted(store.list_dir("runs/"), reverse=True)
    run_pick = mo.ui.dropdown(
        options=runs, value=(runs[0] if runs else None), label="Acquired run"
    )
    mo.vstack([mo.md(f"**{len(runs)} run(s)** in the bucket:"), run_pick])
    return run_pick, runs


@app.cell
def _(Manifest, mo, run_pick, store):
    mo.stop(not run_pick.value, mo.md("*No run selected.*").callout(kind="warn"))
    manifest = Manifest.load(store, f"runs/{run_pick.value}/manifest.json")
    mo.stop(manifest is None, mo.md("*Manifest not found for this run.*").callout(kind="warn"))

    def _composable(steps):
        def has(step, key):
            return bool(steps.get(step, {}).get("keys", {}).get(key))

        return (
            has("texture", "artifact")
            and has("collision-preview", "artifact")
            and has("embedded-physics", "file")
        )

    rows = [
        {"name": a.get("asset_name") or aid, "id": aid}
        for aid, a in manifest.data["assets"].items()
        if _composable(a.get("steps", {}))
    ]
    assets = mo.ui.table(rows, selection="single", page_size=12, label="Composable assets")
    mo.vstack([mo.md(f"**{len(rows)} composable assets** in `{run_pick.value}`:"), assets])
    return assets, manifest


@app.cell
def _(mo):
    run = mo.ui.run_button(label="Compose selected asset")
    run
    return (run,)


@app.cell
def _(assets, compose_asset, manifest, mo, run, settings, store, usd_dir):
    mo.stop(not run.value, mo.md("*Select an asset, then click compose.*").callout(kind="neutral"))
    selection = assets.value
    mo.stop(not selection, mo.md("*No asset selected.*").callout(kind="warn"))

    asset_id = selection[0]["id"]
    with mo.status.spinner(title=f"Composing {asset_id} from R2 …"):
        compose_result = compose_asset(
            asset_id, settings, store=store, run_id=manifest.data["run_id"],
            usd_dir=str(usd_dir), delight=True,
        )
    mo.md(
        f"Composed **{asset_id}** → `{compose_result['status']}` "
        f"(uploaded to `usd/{asset_id}/`)"
    )
    return asset_id, compose_result


@app.cell
def _(
    Path,
    asset_id,
    compose_result,
    manifest,
    mo,
    model_viewer_html,
    store,
    usdz_viewer_html,
):
    import tempfile

    if compose_result["status"] != "done":
        view = mo.md(
            f"**{asset_id}** is not composable "
            f"(`{compose_result['status']}`: {compose_result.get('error', '')})."
        ).callout(kind="warn")
    else:
        steps = manifest.data["assets"][asset_id]["steps"]
        glb_tmp = Path(tempfile.mkdtemp()) / "textured.glb"
        store.download_to(steps["texture"]["keys"]["artifact"], glb_tmp)
        glb = glb_tmp.read_bytes()

        local = Path(compose_result["local"])
        usdz = next(local.glob("*.usdz")).read_bytes()
        png = next(local.glob("*_basecolor.png"), None)

        phys = store.get_json(steps["embedded-physics"]["keys"]["file"]) or {}
        part = ((phys.get("data") or phys).get("parts") or [{}])[0]
        meta = mo.md(
            f"""
            | property | value |
            |---|---|
            | mass (kg) | {part.get("mass")} |
            | material | {part.get("material")} |
            | static / dynamic friction | {part.get("static_friction")} / {part.get("dynamic_friction")} |
            | usdz size | {len(usdz) / 1e6:.1f} MB |
            """
        )

        tabs = mo.ui.tabs(
            {
                "GLB (model-viewer)": mo.iframe(model_viewer_html(glb, usdz)),
                "USDZ (three.js · experimental)": mo.iframe(usdz_viewer_html(usdz)),
            }
        )
        view = mo.vstack(
            [
                tabs,
                mo.download(usdz, filename=f"{asset_id}.usdz", label="⬇ Download .usdz"),
                meta,
                mo.image(str(png), width=256) if png else mo.md("*(untextured)*"),
            ]
        )
    view
    return


if __name__ == "__main__":
    app.run()
