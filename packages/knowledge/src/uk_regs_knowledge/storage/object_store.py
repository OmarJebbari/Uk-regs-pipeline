"""
Object storage (S3 / MinIO) for immutable PDFs and rendered figure images.

Implements WORM-style semantics for source documents: keys are content-hash
based and never overwritten. Figure images are keyed by stable id.
"""

from __future__ import annotations

import abc
from typing import IO

from uk_regs_core import Settings, StorageError, get_settings


class ObjectStore(abc.ABC):
    @abc.abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...

    @abc.abstractmethod
    def get(self, key: str) -> bytes: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def presigned_url(self, key: str, *, expires_s: int = 3600) -> str: ...


class S3ObjectStore(ObjectStore):
    """boto3-backed client, works with both AWS S3 and MinIO."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        import boto3
        from botocore.client import Config

        self._s3 = boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
            config=Config(signature_version="s3v4"),
        )
        self._bucket = self._settings.s3_bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception:
            try:
                self._s3.create_bucket(Bucket=self._bucket)
            except Exception as e:  # pragma: no cover
                raise StorageError(f"Cannot create bucket {self._bucket}: {e}") from e

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
        except Exception as e:
            raise StorageError(f"put failed for {key}: {e}") from e

    def get(self, key: str) -> bytes:
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except Exception as e:
            raise StorageError(f"get failed for {key}: {e}") from e

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_url(self, key: str, *, expires_s: int = 3600) -> str:
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_s,
        )


def document_key(doc_id: str, version: str, sha256: str) -> str:
    """Immutable, content-addressed key for a source PDF."""
    return f"documents/{doc_id}/{version}/{sha256}.pdf"


def figure_key(doc_id: str, version: str, fid: str) -> str:
    return f"figures/{doc_id}/{version}/{fid}.png"


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    return S3ObjectStore(settings)