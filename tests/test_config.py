from usd_pipeline.config import Settings


def test_defaults_present():
    s = Settings(_env_file=None)
    assert s.palatial_base_url == "https://dashboard.palatial.cloud/api/v1"
    assert s.r2_region == "auto"
    assert s.pipeline_concurrency == 8
    assert s.http_max_retries >= 1
    assert s.palatial_cookie == ""
    # R2 endpoint/bucket are not hardcoded — supplied via env / .env
    assert s.r2_endpoint_url == ""
    assert s.r2_bucket == ""


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "other-bucket")
    monkeypatch.setenv("PIPELINE_CONCURRENCY", "3")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "abc")
    s = Settings(_env_file=None)
    assert s.r2_bucket == "other-bucket"
    assert s.pipeline_concurrency == 3
    assert s.r2_access_key_id == "abc"
