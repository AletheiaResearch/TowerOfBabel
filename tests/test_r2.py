import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from usd_pipeline.r2 import R2Store


@pytest.fixture
def r2():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield R2Store(bucket="test-bucket", client=client)


def test_verify_bucket_ok(r2):
    r2.verify_bucket()  # should not raise


def test_put_and_get_json(r2):
    n = r2.put_json("runs/x/manifest.json", {"hello": "world"})
    assert n > 0
    assert r2.exists("runs/x/manifest.json")
    assert r2.get_json("runs/x/manifest.json") == {"hello": "world"}


def test_get_json_missing_returns_none(r2):
    assert r2.get_json("missing.json") is None
    assert r2.exists("missing.json") is False


def test_put_bytes_and_stream(r2):
    assert r2.put_bytes("a/b.bin", b"12345") == 5
    assert r2.put_stream("c/d.bin", iter([b"ab", b"cde"])) == 5
    body = r2._client.get_object(Bucket="test-bucket", Key="c/d.bin")["Body"].read()
    assert body == b"abcde"


def test_verify_bucket_missing_raises():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        store = R2Store(bucket="does-not-exist", client=client)
        with pytest.raises(ClientError):
            store.verify_bucket()


def test_from_settings_incomplete_config_raises():
    from usd_pipeline.config import Settings

    settings = Settings(_env_file=None)  # endpoint/bucket/credentials all empty
    with pytest.raises(ValueError, match="R2 configuration is incomplete"):
        R2Store.from_settings(settings)


def test_download_to(r2, tmp_path):
    r2.put_bytes("a/mesh.glb", b"BINARYMESH")
    dest = tmp_path / "sub" / "mesh.glb"
    r2.download_to("a/mesh.glb", dest)
    assert dest.read_bytes() == b"BINARYMESH"


def test_delete_prefix(r2):
    r2.put_bytes("usd/a1/a1.usda", b"u")
    r2.put_bytes("usd/a1/a1.usdz", b"z")
    r2.put_bytes("usd/a2/a2.usda", b"keep")
    n = r2.delete_prefix("usd/a1/")
    assert n == 2
    assert not r2.exists("usd/a1/a1.usda")
    assert r2.exists("usd/a2/a2.usda")
