"""Cloudflare R2 object store via the S3-compatible API (boto3)."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_MISSING_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
_SPOOL_MAX_BYTES = 32 * 1024 * 1024  # keep small artifacts in RAM, spill larger to disk


class R2Store:
    """S3-compatible store backed by Cloudflare R2. boto3 low-level clients are thread-safe."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str = "auto",
        client=None,
    ):
        self.bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @classmethod
    def from_settings(cls, settings) -> R2Store:
        missing = [
            name
            for name, value in (
                ("R2_ENDPOINT_URL", settings.r2_endpoint_url),
                ("R2_BUCKET", settings.r2_bucket),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"R2 configuration is incomplete; set {', '.join(missing)} (e.g. in "
                ".env, read relative to the current directory), or pass --local "
                "to write to a directory instead of R2."
            )
        return cls(
            bucket=settings.r2_bucket,
            endpoint_url=settings.r2_endpoint_url,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
            region=settings.r2_region,
        )

    def verify_bucket(self) -> None:
        self._client.head_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return len(data)

    def put_stream(self, key: str, chunks: Iterable[bytes], content_type: str | None = None) -> int:
        total = 0
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as buf:
            for chunk in chunks:
                buf.write(chunk)
                total += len(chunk)
            buf.seek(0)
            extra = {"ContentType": content_type} if content_type else {}
            self._client.upload_fileobj(buf, self.bucket, key, ExtraArgs=extra)
        return total

    def put_json(self, key: str, obj: object) -> int:
        data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        return self.put_bytes(key, data, "application/json")

    def get_json(self, key: str) -> object | None:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in _MISSING_CODES:
                return None
            raise
        return json.loads(resp["Body"].read().decode("utf-8"))

    def download_to(self, key: str, dest) -> None:
        parent = os.path.dirname(str(dest))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._client.download_file(self.bucket, key, str(dest))

    def delete_prefix(self, prefix: str) -> int:
        paginator = self._client.get_paginator("list_objects_v2")
        deleted = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            batch = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if batch:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
                deleted += len(batch)
        return deleted

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in _MISSING_CODES:
                return False
            raise
