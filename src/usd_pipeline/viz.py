"""Self-contained HTML previews of composed assets, for notebooks.

Pure functions that return standalone HTML documents (CDN scripts + base64 data URIs of
the asset bytes). Render them in a sandboxed iframe so the scripts run — e.g. in marimo:

    import marimo as mo
    from usd_pipeline import model_viewer_html
    mo.iframe(model_viewer_html(glb_bytes, usdz_bytes))

``model_viewer_html`` renders the textured GLB with Google's ``<model-viewer>`` (robust on
every desktop browser; the optional USDZ is wired as iOS AR Quick Look). ``usdz_viewer_html``
is a best-effort attempt to render the actual ``.usdz`` via three.js' experimental
``USDZLoader`` — it may render incompletely and shows an error in-frame if it can't.
"""

from __future__ import annotations

import base64
import json

_MODEL_VIEWER_CDN = "https://unpkg.com/@google/model-viewer/dist/model-viewer.min.js"
_THREE_CDN = "https://unpkg.com/three@0.160.0/build/three.module.js"
_THREE_ADDONS = "https://unpkg.com/three@0.160.0/examples/jsm/"


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def model_viewer_html(glb: bytes, usdz: bytes | None = None, *, height: int = 480) -> str:
    """HTML rendering ``glb`` with <model-viewer>; ``usdz`` (if given) wired for iOS AR."""
    glb_uri = _data_uri(glb, "model/gltf-binary")
    ar_attrs = ""
    if usdz is not None:
        ar_attrs = f'ios-src="{_data_uri(usdz, "model/vnd.usdz+zip")}" ar ar-modes="quick-look"'
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<script type="module" src="{_MODEL_VIEWER_CDN}"></script>
<style>html,body{{margin:0}} model-viewer{{width:100%;height:{height}px;background:#15151a}}</style>
</head><body>
<model-viewer src="{glb_uri}" {ar_attrs} camera-controls auto-rotate shadow-intensity="1"
  exposure="1.0" environment-image="neutral" style="--poster-color:transparent"></model-viewer>
</body></html>"""


def usdz_viewer_html(usdz: bytes, *, height: int = 480) -> str:
    """Best-effort HTML rendering the actual ``.usdz`` via three.js USDZLoader (experimental)."""
    b64 = base64.b64encode(usdz).decode("ascii")
    # The three.js example modules import the bare specifiers "three" / "three/addons/";
    # an import map is REQUIRED or the module load silently fails (blank viewer).
    importmap = json.dumps({"imports": {"three": _THREE_CDN, "three/addons/": _THREE_ADDONS}})
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<script type="importmap">{importmap}</script>
<style>html,body{{margin:0;background:#15151a}} #wrap{{width:100%;height:{height}px}}
#err{{color:#f88;font:14px sans-serif;padding:1em}}</style>
</head><body><div id="wrap"></div>
<script type="module">
import * as THREE from "three";
import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";
import {{ USDZLoader }} from "three/addons/loaders/USDZLoader.js";
const wrap = document.getElementById("wrap");
const w = wrap.clientWidth || 640, h = {height};
const renderer = new THREE.WebGLRenderer({{antialias:true, alpha:true}});
renderer.setSize(w, h); wrap.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.add(new THREE.HemisphereLight(0xffffff, 0x333344, 3.0));
const cam = new THREE.PerspectiveCamera(50, w/h, 0.001, 100);
cam.position.set(0.25, 0.2, 0.35);
const controls = new OrbitControls(cam, renderer.domElement);
const bytes = Uint8Array.from(atob("{b64}"), c => c.charCodeAt(0));
try {{
  const model = new USDZLoader().parse(bytes.buffer);
  const box = new THREE.Box3().setFromObject(model);
  const c = box.getCenter(new THREE.Vector3()); model.position.sub(c);
  controls.target.set(0, 0, 0);
  const r = box.getSize(new THREE.Vector3()).length() || 1;
  cam.position.set(r*0.6, r*0.45, r*0.8); controls.update();
  scene.add(model);
}} catch (e) {{
  wrap.innerHTML = '<div id="err">USDZ render failed: ' + e + '</div>';
}}
(function loop(){{
  requestAnimationFrame(loop); controls.update(); renderer.render(scene, cam);
}})();
</script></body></html>"""
