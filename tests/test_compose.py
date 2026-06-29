import pytest
from pxr import UsdGeom

from usd_pipeline.compose import _pick_part, _safe_name, _unwrap, _up_axis_from_gravity


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
