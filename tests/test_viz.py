import base64

from usd_pipeline.viz import model_viewer_html, usdz_viewer_html


def test_model_viewer_html_embeds_glb():
    html = model_viewer_html(b"GLBDATA")
    assert "<model-viewer" in html
    assert "data:model/gltf-binary;base64," in html
    assert base64.b64encode(b"GLBDATA").decode() in html
    assert "ios-src" not in html  # no usdz supplied


def test_model_viewer_html_with_usdz_wires_ar():
    html = model_viewer_html(b"GLB", b"USDZ")
    assert "ios-src=" in html
    assert "data:model/vnd.usdz+zip;base64," in html
    assert 'ar-modes="quick-look"' in html


def test_usdz_viewer_html_embeds_usdz():
    html = usdz_viewer_html(b"USDZBYTES")
    assert "USDZLoader" in html
    assert base64.b64encode(b"USDZBYTES").decode() in html
    # the import map is required for the bare "three" / "three/addons/" specifiers to resolve
    assert '<script type="importmap">' in html
    assert '"three":' in html and '"three/addons/":' in html
    assert 'from "three"' in html
