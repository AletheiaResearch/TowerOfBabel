import json
from pathlib import Path

import pytest
import trimesh
from pxr import Usd, UsdGeom, UsdPhysics

from usd_pipeline.compose import (
    _pick_part,
    _safe_name,
    _unwrap,
    _up_axis_from_gravity,
    compose_usd,
)


def _box_glb(path: Path) -> str:
    trimesh.creation.box(extents=(0.1, 0.1, 0.1)).export(path)
    return str(path)


_PHYSICS_JSON = {
    "parts": [
        {
            "is_structural_root": True,
            "mass": 0.2,
            "static_friction": 0.5,
            "newton": {"collisions": {}, "simulation_scene": {}},
        }
    ]
}


def test_unwrap_direct():
    assert _unwrap({"parts": [1]}) == {"parts": [1]}


def test_unwrap_data_wrapper():
    assert _unwrap({"_id": "x", "data": {"parts": [1]}}) == {"parts": [1]}


def test_unwrap_nested():
    assert _unwrap({"foo": {"parts": [1]}}) == {"parts": [1]}


def test_unwrap_missing_raises():
    with pytest.raises(ValueError):
        _unwrap({"x": 1})


def test_pick_structural_root():
    spec = {"parts": [{"part_id": "a"}, {"part_id": "b", "is_structural_root": True}]}
    assert _pick_part(spec)["part_id"] == "b"


def test_pick_first_when_no_root():
    spec = {"parts": [{"part_id": "a"}, {"part_id": "b"}]}
    assert _pick_part(spec)["part_id"] == "a"


def test_pick_empty_raises():
    with pytest.raises(ValueError):
        _pick_part({"parts": []})


def test_up_axis_from_gravity():
    assert _up_axis_from_gravity([0, 0, -9.81]) == UsdGeom.Tokens.z
    assert _up_axis_from_gravity([0, -9.81, 0]) == UsdGeom.Tokens.y
    assert _up_axis_from_gravity([-9.81, 0, 0]) == UsdGeom.Tokens.x
    assert _up_axis_from_gravity([0, 0, 0]) == UsdGeom.Tokens.z


def test_safe_name():
    assert _safe_name("D00738 Smartphone") == "D00738_Smartphone"
    assert _safe_name("123abc") == "Asset_123abc"
    assert _safe_name("") == "Asset_"
    assert _safe_name(None) == "Asset_"


def test_compose_usd_physics(tmp_path):
    tex = _box_glb(tmp_path / "t.glb")
    col = _box_glb(tmp_path / "c.glb")
    phys = tmp_path / "p.json"
    phys.write_text(json.dumps(_PHYSICS_JSON))
    out = compose_usd(tex, col, str(phys), str(tmp_path / "full.usda"), usdz=True, verbose=False)

    assert out["usdz"] and Path(out["usdz"]).exists()
    text = Path(out["usd"]).read_text()
    assert "physics:approximation" in text  # collider authored
    stage = Usd.Stage.Open(out["usd"])
    assert stage.GetPrimAtPath("/PhysicsScene").IsValid()
    assert any(p.HasAPI(UsdPhysics.RigidBodyAPI) for p in stage.Traverse())


def test_compose_usd_visual_only(tmp_path):
    tex = _box_glb(tmp_path / "t.glb")
    out = compose_usd(
        tex, out_path=str(tmp_path / "vis.usda"), physics=False, usdz=True, verbose=False
    )

    assert out["usdz"] and Path(out["usdz"]).exists()
    text = Path(out["usd"]).read_text()
    # nothing that three.js USDZLoader chokes on
    assert "physics:approximation" not in text
    assert "PhysicsScene" not in text
    stage = Usd.Stage.Open(out["usd"])
    assert not stage.GetPrimAtPath("/PhysicsScene").IsValid()
    assert any(p.IsA(UsdGeom.Mesh) for p in stage.Traverse())  # render mesh present


def test_compose_usd_physics_requires_inputs(tmp_path):
    tex = _box_glb(tmp_path / "t.glb")
    with pytest.raises(ValueError, match="requires collision_glb"):
        compose_usd(tex, out_path=str(tmp_path / "x.usda"), physics=True)
