"""Interactive Palatial → USD/USDz explorer.

Run it with:   uv run --extra notebook marimo edit notebooks/explore.py

Flow: browse the Palatial library → pick one asset → acquire just that asset's parts →
compose a USD/USDz → preview it (textured GLB via <model-viewer>, plus a best-effort
three.js render of the actual .usdz). Everything is written to a local temp dir using the
public Palatial API — no Cloudflare R2 credentials required.
"""

import marimo

__generated_with = "0.23.11"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md("""
    # Palatial → USD/USDz explorer

    Pick an asset from the library, acquire its parts, compose a rigid-body USD, and
    preview it — all locally, no R2 credentials needed.
    """)
    return


@app.cell
def _():
    import tempfile
    from pathlib import Path

    from usd_pipeline import (
        LocalStore,
        PalatialClient,
        Settings,
        acquire_asset,
        compose_asset,
        compose_usd,
        model_viewer_html,
        usdz_viewer_html,
    )

    settings = Settings(_env_file=None)  # public Palatial API; no R2 needed
    workdir = Path(tempfile.gettempdir()) / "usd-pipeline-notebook"
    store = LocalStore(workdir / "data")
    usd_dir = workdir / "usd"
    preview_dir = workdir / "preview"  # where the GLB / preview USD is staged for the viewer
    return (
        PalatialClient,
        Path,
        acquire_asset,
        compose_asset,
        compose_usd,
        model_viewer_html,
        preview_dir,
        settings,
        store,
        usd_dir,
        usdz_viewer_html,
    )


@app.cell
def _(PalatialClient, settings):
    with PalatialClient(base_url=settings.palatial_base_url) as _client:
        results = _client.search(q="", page=1, limit=812).get("results", [])
    return (results,)


@app.cell
def _(mo, results):
    rows = [
        {
            "name": r.get("asset_name") or r.get("name") or r.get("_id"),
            "categories": r.get("categories"),
            "triangles": r.get("triangle_count"),
            "id": r.get("_id"),
        }
        for r in results
    ]
    library = mo.ui.table(rows, selection="single", page_size=12, label="Library")
    mo.md(f"**{len(rows)} assets.** Search/sort, then select one row:")
    return (library,)


@app.cell
def _(library):
    library
    return


@app.cell
def _(mo):
    run = mo.ui.run_button(label="① Acquire + compose the selected asset")
    run
    return (run,)


@app.cell
def _(
    acquire_asset,
    compose_asset,
    library,
    mo,
    run,
    settings,
    store,
    usd_dir,
):
    mo.stop(
        not run.value,
        mo.md("*Select an asset above, then click the button.*").callout(kind="neutral"),
    )
    selection = library.value
    mo.stop(not selection, mo.md("*No asset selected.*").callout(kind="warn"))

    asset_id = selection[0]["id"]
    with mo.status.spinner(title=f"Acquiring {asset_id} …"):
        manifest = acquire_asset(asset_id, settings, store=store)
    with mo.status.spinner(title="Composing USD/USDz …"):
        compose_result = compose_asset(
            asset_id, settings, store=store, run_id=manifest.data["run_id"],
            usd_dir=str(usd_dir), delight=True,
        )
    mo.md(f"Acquired & composed **{asset_id}** → status `{compose_result['status']}`")
    return asset_id, compose_result, manifest


@app.cell
def _(
    Path,
    asset_id,
    compose_result,
    compose_usd,
    manifest,
    mo,
    model_viewer_html,
    preview_dir,
    store,
    usdz_viewer_html,
):
    if compose_result["status"] != "done":
        view = mo.md(
            f"**{asset_id}** is not composable "
            f"(`{compose_result['status']}`: {compose_result.get('error', '')})."
        ).callout(kind="warn")
    else:
        steps = manifest.data["assets"][asset_id]["steps"]

        # textured render mesh (the GLB the USD wraps), staged locally for the viewer
        preview_dir.mkdir(parents=True, exist_ok=True)
        glb_tmp = preview_dir / "textured.glb"
        store.download_to(steps["texture"]["keys"]["artifact"], glb_tmp)
        glb = glb_tmp.read_bytes()

        # composed outputs (local usd dir): full physics .usdz for download / AR
        local = Path(compose_result["local"])
        usdz = next(local.glob("*.usdz")).read_bytes()
        png = next(local.glob("*_basecolor.png"), None)

        # three.js USDZLoader can't parse PhysX schemas -> feed it a visual-only USD
        compose_usd(
            str(glb_tmp), out_path=str(preview_dir / "preview.usda"),
            physics=False, usdz=True, delight=True, verbose=False,
        )
        preview_usdz = (preview_dir / "preview.usdz").read_bytes()

        # physics summary
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
                "USD (three.js · visual-only)": mo.iframe(usdz_viewer_html(preview_usdz)),
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
