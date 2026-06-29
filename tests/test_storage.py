from usd_pipeline.storage import LocalStore


def test_put_and_get_json(tmp_path):
    store = LocalStore(tmp_path)
    n = store.put_json("runs/x/manifest.json", {"a": 1})
    assert n > 0
    assert store.exists("runs/x/manifest.json")
    assert store.get_json("runs/x/manifest.json") == {"a": 1}


def test_get_json_missing_returns_none(tmp_path):
    store = LocalStore(tmp_path)
    assert store.get_json("nope.json") is None
    assert store.exists("nope.json") is False


def test_put_bytes_creates_parent_dirs(tmp_path):
    store = LocalStore(tmp_path)
    n = store.put_bytes("a/b/c/file.bin", b"hello")
    assert n == 5
    assert (tmp_path / "a/b/c/file.bin").read_bytes() == b"hello"


def test_put_stream(tmp_path):
    store = LocalStore(tmp_path)
    n = store.put_stream("d/e/blob.bin", iter([b"foo", b"bar", b"baz"]))
    assert n == 9
    assert (tmp_path / "d/e/blob.bin").read_bytes() == b"foobarbaz"


def test_download_to_creates_parents(tmp_path):
    store = LocalStore(tmp_path / "store")
    store.put_bytes("a/b.glb", b"MESH")
    dest = tmp_path / "out" / "sub" / "x.glb"
    store.download_to("a/b.glb", dest)
    assert dest.read_bytes() == b"MESH"


def test_delete_prefix(tmp_path):
    store = LocalStore(tmp_path)
    store.put_bytes("usd/a1/a1.usda", b"u")
    store.put_bytes("usd/a1/a1.usdz", b"z")
    store.put_bytes("usd/a2/a2.usda", b"keep")
    n = store.delete_prefix("usd/a1/")
    assert n == 2
    assert not store.exists("usd/a1/a1.usda")
    assert store.exists("usd/a2/a2.usda")
    assert store.delete_prefix("usd/missing/") == 0
