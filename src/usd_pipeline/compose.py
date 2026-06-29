"""Compose Palatial asset parts into an Isaac-Sim-ready rigid-body USD/USDz.

This is purely the composition step: given a textured render mesh, a convex-hull
collision mesh, and Palatial's "newton" physics spec, it authors a single rigid-body
USD stage (+ optional self-contained .usdz). Acquisition fetches these inputs; this
turns them into USD.

Non-obvious choices (each deliberate):
  * The render mesh is tagged ``purpose="default"`` — real-time viewers (three.js USDZ,
    Hydra GL, QuickLook) render only ``default``/``proxy`` and skip ``render``/``guide``;
    tagging the visual mesh ``render`` leaves the viewport empty.
  * Collision hulls are imported verbatim with ``approximation="convexHull"`` — the GLB
    is already a convex decomposition, so re-decomposing would be wrong.
  * The stage up-axis comes from the physics gravity vector and the GLB geometry is kept
    in its native frame (no glTF Y-up -> Z-up rotation): Palatial authors mesh,
    collision, COM, inertia and gravity in one frame, so rotating would tip the asset.
  * Material uses the source glTF PBR factors (roughness/metallic/baseColor), not
    invented values.
  * Inertia: author the predicted tensor when present, else let PhysX derive it from the
    accurate hull geometry + mass.
  * PhysX-only knobs (CCD, contact/rest offset, combine modes, solver rate) are written
    as correctly-named raw attributes plus the matching (unregistered) API-schema names,
    so omni.physx reads them and they schema-validate once Isaac registers the schemas.
  * ``delight`` clips the baked specular highlights Palatial paints into albedos so the
    asset looks right in viewers you do not control.
  * ``physics=False`` authors a visual-only USD (render mesh + material, no PhysX) — for
    web viewers (e.g. three.js USDZLoader) that can't parse the physics schemas.

Scope: a single rigid body (``physics_type``/object type "rigid_bodies"). Articulated
assets (non-empty joint relations) need joint + drive authoring this does not do.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageChops, ImageFilter
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, UsdUtils, Vt

_UP_AXIS_TOKENS = (UsdGeom.Tokens.x, UsdGeom.Tokens.y, UsdGeom.Tokens.z)


# --------------------------------------------------------------------------- #
# physics-spec helpers
# --------------------------------------------------------------------------- #
def _unwrap(doc: dict) -> dict:
    """Return the dict holding ``parts`` (handles a {_id, data:{...}} DB wrapper)."""
    if "parts" in doc:
        return doc
    data = doc.get("data")
    if isinstance(data, dict) and "parts" in data:
        return data
    for value in doc.values():
        if isinstance(value, dict) and "parts" in value:
            return value
    raise ValueError("physics JSON has no 'parts' array")


def _pick_part(spec: dict) -> dict:
    """Pick the structural-root part (the rigid body to author)."""
    parts = spec.get("parts") or []
    if not parts:
        raise ValueError("physics JSON 'parts' is empty")
    if len(parts) > 1:
        print(
            f"[compose] WARNING: {len(parts)} parts present; composing the structural "
            "root as one rigid body. Articulation (joints) is not authored.",
            file=sys.stderr,
        )
    for part in parts:
        if part.get("is_structural_root"):
            return part
    return parts[0]


def _up_axis_from_gravity(gravity: Any) -> str:
    """Up-axis = the axis the gravity vector points along (defaults to Z)."""
    g = np.asarray(gravity, dtype=float)
    if not np.any(g):
        return UsdGeom.Tokens.z
    return _UP_AXIS_TOKENS[int(np.argmax(np.abs(g)))]


def _safe_name(value: str | None) -> str:
    """Sanitize a string into a valid USD prim name."""
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", (value or "").strip())
    if cleaned and not cleaned[0].isdigit():
        return cleaned
    return "Asset_" + cleaned


# --------------------------------------------------------------------------- #
# geometry / material helpers
# --------------------------------------------------------------------------- #
def _load_geoms(path: str) -> Iterator[tuple[str, trimesh.Trimesh]]:
    """Yield (name, Trimesh) with node transforms baked into the vertices."""
    scene = trimesh.load(path, process=False)
    if isinstance(scene, trimesh.Scene):
        for node in scene.graph.nodes_geometry:
            transform, geom_name = scene.graph[node]
            geom = scene.geometry[geom_name].copy()
            geom.apply_transform(transform)
            yield geom_name, geom
    else:
        yield "mesh", scene


def _delight(
    img: Image.Image, knee: float = 0.12, ceil: float = 0.17, median: int = 11
) -> Image.Image:
    """Clip baked specular highlights from an albedo, preserving the tonal split."""
    img = ImageChops.darker(img, img.filter(ImageFilter.MedianFilter(size=median)))
    a = np.asarray(img.convert("RGB"), np.float32) / 255.0
    luma = a @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    comp = np.where(luma <= knee, luma, knee + (luma - knee) / (1.0 - knee) * (ceil - knee))
    scale = np.divide(comp, luma, out=np.ones_like(luma), where=luma > 1e-6)
    return Image.fromarray((np.clip(a * scale[..., None], 0, 1) * 255).astype(np.uint8))


def _extract_material(tm: trimesh.Trimesh) -> tuple[Image.Image | None, float, float, list[float]]:
    """Return (baseColor image | None, roughness, metallic, baseColorFactor[3])."""
    mat = getattr(tm.visual, "material", None)
    roughness = getattr(mat, "roughnessFactor", None)
    metallic = getattr(mat, "metallicFactor", None)
    rough = 0.5 if roughness is None else float(roughness)
    metal = 0.0 if metallic is None else float(metallic)

    raw = getattr(mat, "baseColorFactor", None)
    factor = [1.0, 1.0, 1.0, 1.0] if raw is None else list(raw)
    # glTF factors are 0..1; some loaders surface 0..255 — normalize the latter.
    if max(factor) > 1.5:
        bcf = [c / 255.0 for c in factor[:3]]
    else:
        bcf = [float(c) for c in factor[:3]]

    tex = getattr(mat, "baseColorTexture", None)
    img = tex.convert("RGB") if isinstance(tex, Image.Image) else None
    return img, rough, metal, bcf


def _author_mesh(
    stage: Usd.Stage, path: str, tm: trimesh.Trimesh, flip_v: bool, purpose: str
) -> UsdGeom.Mesh:
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr([Gf.Vec3f(*map(float, v)) for v in tm.vertices])
    mesh.CreateFaceVertexCountsAttr([3] * len(tm.faces))
    mesh.CreateFaceVertexIndicesAttr(tm.faces.reshape(-1).tolist())
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    lo, hi = tm.bounds
    mesh.CreateExtentAttr([Gf.Vec3f(*map(float, lo)), Gf.Vec3f(*map(float, hi))])
    try:
        mesh.CreateNormalsAttr([Gf.Vec3f(*map(float, n)) for n in tm.vertex_normals])
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    except Exception:  # noqa: BLE001 - degenerate meshes may lack normals; not fatal
        pass

    uv = getattr(tm.visual, "uv", None)
    if uv is not None and len(uv):
        uv = np.asarray(uv, np.float32).copy()
        if flip_v:  # glTF V-origin is top-left; USD is bottom-left
            uv[:, 1] = 1.0 - uv[:, 1]
        primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
            "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex
        )
        primvar.Set(Vt.Vec2fArray([Gf.Vec2f(float(u), float(v)) for u, v in uv]))

    mesh.CreatePurposeAttr(purpose)
    return mesh


def _add_api_schemas(prim: Usd.Prim, names: list[str]) -> None:
    """Append (possibly unregistered) PhysX API-schema names so Isaac recognizes them."""
    op = prim.GetMetadata("apiSchemas") or Sdf.TokenListOp()
    items = list(op.GetAddedOrExplicitItems())
    items += [n for n in names if n not in items]
    prim.SetMetadata("apiSchemas", Sdf.TokenListOp.CreateExplicit(items))


# --------------------------------------------------------------------------- #
# physics authoring (skipped entirely for a visual-only USD)
# --------------------------------------------------------------------------- #
def _author_physics(stage, name, rb, part, coll, sim, *, use_tensor, collision_glb, log) -> None:
    """Author the rigid body, mass, physics material, and convex-hull colliders."""
    UsdPhysics.RigidBodyAPI.Apply(rb)
    mass_api = UsdPhysics.MassAPI.Apply(rb)
    mass = float(part.get("mass") or 0.0)
    if mass > 0:
        mass_api.CreateMassAttr(mass)  # else PhysX derives from density + volume
    if use_tensor:
        mass_api.CreateDiagonalInertiaAttr(Gf.Vec3f(*map(float, part["inertia"])))
        if part.get("center_of_mass"):
            mass_api.CreateCenterOfMassAttr(Gf.Vec3f(*map(float, part["center_of_mass"])))
        axes = part.get("inertia_axes")
        if axes and len(axes) == 4:
            # GfQuatf is (w, x, y, z); Palatial 'inertia_axes' is (w, x, y, z).
            mass_api.CreatePrincipalAxesAttr(Gf.Quatf(*map(float, axes)))
    rb.CreateAttribute("physxRigidBody:enableCCD", Sdf.ValueTypeNames.Bool).Set(
        bool(sim.get("enable_ccd", True))
    )
    _add_api_schemas(rb, ["PhysxRigidBodyAPI"])

    pmat = UsdShade.Material.Define(stage, f"/{name}/PhysicsMaterials/body")
    pm = pmat.GetPrim()
    pm_api = UsdPhysics.MaterialAPI.Apply(pm)
    pm_api.CreateStaticFrictionAttr(float(part.get("static_friction", 0.5)))
    pm_api.CreateDynamicFrictionAttr(float(part.get("dynamic_friction", 0.5)))
    pm_api.CreateRestitutionAttr(float(part.get("restitution", 0.0)))
    if part.get("density"):
        pm_api.CreateDensityAttr(float(part["density"]))
    pm.CreateAttribute("physxMaterial:frictionCombineMode", Sdf.ValueTypeNames.Token).Set(
        part.get("friction_combine_mode", "average")
    )
    pm.CreateAttribute("physxMaterial:restitutionCombineMode", Sdf.ValueTypeNames.Token).Set(
        part.get("restitution_combine_mode", "average")
    )
    _add_api_schemas(pm, ["PhysxMaterialAPI"])

    UsdGeom.Scope.Define(stage, f"/{name}/Collisions")
    hull_count = 0
    for hull_name, geom in _load_geoms(collision_glb):
        hull_path = f"/{name}/Collisions/{_safe_name(hull_name)}"
        _author_mesh(stage, hull_path, geom, flip_v=False, purpose=UsdGeom.Tokens.guide)
        hull_prim = stage.GetPrimAtPath(hull_path)
        UsdPhysics.CollisionAPI.Apply(hull_prim)
        UsdPhysics.MeshCollisionAPI.Apply(hull_prim).CreateApproximationAttr(
            UsdPhysics.Tokens.convexHull  # already decomposed: do NOT re-decompose
        )
        hull_prim.CreateAttribute("physxCollision:contactOffset", Sdf.ValueTypeNames.Float).Set(
            float(coll.get("contact_offset", 0.002))
        )
        hull_prim.CreateAttribute("physxCollision:restOffset", Sdf.ValueTypeNames.Float).Set(
            float(coll.get("rest_offset", 0.0))
        )
        _add_api_schemas(hull_prim, ["PhysxCollisionAPI"])
        UsdShade.MaterialBindingAPI.Apply(hull_prim).Bind(
            pmat, UsdShade.Tokens.weakerThanDescendants, "physics"
        )
        hull_count += 1
    log(f"[compose] colliders: {hull_count} convex hull(s)")


def _author_physics_scene(stage, grav, sim) -> None:
    scene = UsdPhysics.Scene.Define(stage, "/PhysicsScene")
    gv = Gf.Vec3f(*map(float, grav))
    mag = float(gv.GetLength())
    scene.CreateGravityDirectionAttr(gv / mag if mag else Gf.Vec3f(0, 0, -1))
    scene.CreateGravityMagnitudeAttr(mag or 9.81)
    sp = scene.GetPrim()
    sp.CreateAttribute("physxScene:timeStepsPerSecond", Sdf.ValueTypeNames.UInt).Set(
        int(sim.get("simulation_steps_per_second", 60))
    )
    sp.CreateAttribute("physxScene:enableCCD", Sdf.ValueTypeNames.Bool).Set(
        bool(sim.get("enable_ccd", True))
    )
    _add_api_schemas(sp, ["PhysxSceneAPI"])


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def compose_usd(
    textured_glb: str,
    collision_glb: str | None = None,
    physics_json: str | None = None,
    out_path: str = "asset.usda",
    *,
    physics: bool = True,
    inertia: str = "auto",
    delight: bool = False,
    flip_v: bool = True,
    usdz: bool = False,
    name: str | None = None,
    verbose: bool = True,
) -> dict[str, str | None]:
    """Compose a rigid-body USD from Palatial parts.

    Args:
        textured_glb: render mesh GLB (real-world scale, baked PBR material).
        collision_glb: convex-hull decomposition GLB (1..N hull meshes). Required when
            ``physics=True``; ignored when ``physics=False``.
        physics_json: Palatial 'newton' physics spec (mass/inertia/friction/CCD/...).
            Required when ``physics=True``; ignored when ``physics=False``.
        out_path: output ``.usd`` / ``.usda`` / ``.usdc`` path; a ``<stem>_basecolor.png``
            is written alongside it.
        physics: author rigid body, mass, physics material, collision hulls, and physics
            scene. ``False`` -> visual-only USD (render mesh + material) for web viewers
            (e.g. three.js USDZLoader) that can't parse the PhysX schemas.
        inertia: ``"auto"`` (tensor if present, else PhysX-derive) | ``"predicted"`` |
            ``"derive"``.
        delight: clip baked specular highlights out of the albedo (cross-viewer fix).
        flip_v: flip texture V (set False if the texture appears vertically mirrored).
        usdz: also write a self-contained ``.usdz`` package.
        name: override the prim/asset name.

    Returns:
        ``{"usd": path, "texture": path | None, "usdz": path | None}``.
    """

    def log(*args: object) -> None:
        if verbose:
            print(*args, file=sys.stderr)

    if physics:
        if not (collision_glb and physics_json):
            raise ValueError("physics=True requires collision_glb and physics_json")
        with open(physics_json) as fh:
            spec = _unwrap(json.load(fh))
        part = _pick_part(spec)
        newton = part.get("newton", {})
        coll = newton.get("collisions", {})
        sim = newton.get("simulation_scene", {})
        grav = sim.get("gravitational_acceleration", [0, 0, -9.81])
    else:
        spec, part, coll, sim = {}, {}, {}, {}
        grav = [0, 0, -9.81]

    name = _safe_name(name or part.get("part_name") or spec.get("base_link") or "Asset")
    out_path = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_path) or "."
    stem = os.path.splitext(os.path.basename(out_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    for stale in (out_path, os.path.splitext(out_path)[0] + ".usdz"):  # idempotent re-runs
        if os.path.exists(stale):
            os.remove(stale)

    has_tensor = part.get("inertia") is not None
    if physics and inertia == "predicted" and not has_tensor:
        raise ValueError("inertia='predicted' but the JSON tensor is null")
    use_tensor = inertia == "predicted" or (inertia == "auto" and has_tensor)
    log(f"[compose] {name}  physics={physics} inertia={'predicted' if use_tensor else 'derived'}")

    # --- stage -------------------------------------------------------------- #
    stage = Usd.Stage.CreateNew(out_path)
    UsdGeom.SetStageUpAxis(stage, _up_axis_from_gravity(grav))
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetMetadata("kilogramsPerUnit", 1.0)

    root = UsdGeom.Xform.Define(stage, f"/{name}")
    stage.SetDefaultPrim(root.GetPrim())
    rb = root.GetPrim()

    if physics:
        _author_physics(
            stage,
            name,
            rb,
            part,
            coll,
            sim,
            use_tensor=use_tensor,
            collision_glb=collision_glb,
            log=log,
        )

    # --- render mesh + visual material (always) ----------------------------- #
    UsdGeom.Scope.Define(stage, f"/{name}/Render")
    render_geoms = list(_load_geoms(textured_glb))
    if len(render_geoms) > 1:
        log(f"[compose] WARNING: textured GLB has {len(render_geoms)} meshes; using the first.")
    _, render_tm = render_geoms[0]
    render_mesh = _author_mesh(
        stage, f"/{name}/Render/mesh", render_tm, flip_v=flip_v, purpose=UsdGeom.Tokens.default_
    )
    img, rough, metal, bcf = _extract_material(render_tm)
    log(
        f"[compose] material roughness={rough:.3f} metallic={metal:.3f} "
        f"baseColorFactor={[round(c, 3) for c in bcf]} textured={img is not None}"
    )

    look = UsdShade.Material.Define(stage, f"/{name}/Looks/surface")
    shader = UsdShade.Shader.Define(stage, f"/{name}/Looks/surface/PreviewSurface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(rough)
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(metal)

    tex_path: str | None = None
    if img is not None:
        if delight:
            img = _delight(img)
            log("[compose] de-light: baked highlights clipped from albedo")
        tex_path = os.path.join(out_dir, f"{stem}_basecolor.png")
        img.save(tex_path)
        st_reader = UsdShade.Shader.Define(stage, f"/{name}/Looks/surface/stReader")
        st_reader.CreateIdAttr("UsdPrimvarReader_float2")
        st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        tex = UsdShade.Shader.Define(stage, f"/{name}/Looks/surface/baseColor")
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(f"./{os.path.basename(tex_path)}")
        tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            st_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)
        )
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
        tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")  # match glTF sampler
        tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
        tex.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(
            Gf.Vec4f(bcf[0], bcf[1], bcf[2], 1.0)  # baseColorFactor tint
        )
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            tex.CreateOutput("rgb", Sdf.ValueTypeNames.Color3f)
        )
    else:
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(bcf[0], bcf[1], bcf[2])
        )

    look.CreateSurfaceOutput().ConnectToSource(
        shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )
    UsdShade.MaterialBindingAPI.Apply(render_mesh.GetPrim()).Bind(look)

    if physics:
        _author_physics_scene(stage, grav, sim)

    stage.GetRootLayer().Save()
    log(f"[compose] wrote {out_path}")
    result: dict[str, str | None] = {"usd": out_path, "texture": tex_path, "usdz": None}

    if usdz:
        usdz_path = os.path.splitext(out_path)[0] + ".usdz"
        UsdUtils.CreateNewUsdzPackage(out_path, usdz_path)
        result["usdz"] = usdz_path
        log(f"[compose] packaged {usdz_path}")
    return result
