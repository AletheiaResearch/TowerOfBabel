"""Environment-driven configuration for the pipeline."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Palatial API
    palatial_base_url: str = "https://dashboard.palatial.cloud/api/v1"
    palatial_cookie: str = ""  # optional full Cookie header override

    # Cloudflare R2 (S3-compatible) — all supplied via env / .env (see .env.example)
    r2_endpoint_url: str = ""  # https://<account-id>.r2.cloudflarestorage.com
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_region: str = "auto"

    # Run tuning
    pipeline_concurrency: int = 8
    http_timeout: float = 60.0
    http_max_retries: int = 4
    manifest_flush_every: int = 8  # ~one asset; bounds progress lost on a hard kill

    # USD composition step
    pipeline_usd_dir: str = "~/usd"  # global local output dir; <usd_dir>/<assetId>/
    compose_concurrency: int = 4
