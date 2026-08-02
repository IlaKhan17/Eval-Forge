"""Content-addressed payload storage.

Payloads above the inline threshold go to S3-compatible object storage, keyed by the
SHA-256 of their content. Two properties follow from content addressing:

- **Deduplication is automatic.** A system prompt repeated across ten thousand spans
  is uploaded and stored once. At real trace volumes that is most of the bytes.
- **Writes are idempotent.** Re-ingesting the same batch overwrites identical bytes
  at the identical key, so a retry costs nothing and corrupts nothing.

Keys are prefixed `{project_id}/{sha256}` so a bucket policy or lifecycle rule can be
scoped per tenant, and so a listing cannot enumerate across projects.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import uuid


@dataclass(frozen=True, slots=True)
class StoredPayload:
    sha256: bytes
    bucket: str
    object_key: str
    size_bytes: int
    encoding: str = "gzip"
    content_type: str = "application/json"


class ObjectStore(Protocol):
    """The storage surface the ingestion service needs.

    Narrow on purpose: a protocol this small is trivial to fake in tests, which is
    what lets ingestion be tested without MinIO running.
    """

    bucket: str

    def put(self, key: str, body: bytes, *, content_type: str, encoding: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def presign(self, key: str, *, expires_in: int) -> str: ...

    def delete(self, key: str) -> None: ...


class InMemoryObjectStore:
    """A store for tests and for `--local` runs with no MinIO."""

    def __init__(self, bucket: str = "evalforge-test") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.put_calls = 0

    def put(self, key: str, body: bytes, *, content_type: str, encoding: str) -> None:  # noqa: ARG002
        self.put_calls += 1
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def presign(self, key: str, *, expires_in: int) -> str:
        return f"memory://{self.bucket}/{key}?expires_in={expires_in}"

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class S3ObjectStore:
    """boto3-backed store for MinIO or S3."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        import boto3  # noqa: PLC0415 — lazy so tests need no AWS dependency

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, body: bytes, *, content_type: str, encoding: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ContentEncoding=encoding,
        )

    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return bytes(response["Body"].read())

    def presign(self, key: str, *, expires_in: int) -> str:
        """A short-lived, single-object URL.

        It is a bearer credential for its lifetime, so the window is deliberately
        small and it never covers more than one object.
        """
        return str(
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


def serialize(payload: Any) -> bytes:
    """Canonical JSON, so identical content hashes identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def content_key(project_id: uuid.UUID, digest: bytes) -> str:
    return f"{project_id}/{digest.hex()}"


def store_payload(store: ObjectStore, project_id: uuid.UUID, payload: Any) -> StoredPayload:
    raw = serialize(payload)
    digest = hashlib.sha256(raw).digest()
    key = content_key(project_id, digest)
    body = gzip.compress(raw)
    store.put(key, body, content_type="application/json", encoding="gzip")
    return StoredPayload(sha256=digest, bucket=store.bucket, object_key=key, size_bytes=len(body))


def load_payload(store: ObjectStore, object_key: str) -> Any:
    return json.loads(gzip.decompress(store.get(object_key)))


_store: ObjectStore | None = None


def get_store(settings: Any) -> ObjectStore:
    """Resolve the configured object store, memoized per process.

    Falls back to the in-memory store when no S3 endpoint is configured, so the API
    runs with nothing but Postgres. Large payloads then live only for the process
    lifetime, which is the right trade for a local run and is reported at startup
    rather than discovered later.
    """
    global _store  # noqa: PLW0603 — one client per process
    if _store is not None:
        return _store

    endpoint = getattr(settings, "s3_endpoint", None)
    if not endpoint:
        _store = InMemoryObjectStore(bucket="evalforge-local")
        return _store

    store = S3ObjectStore(
        bucket=settings.s3_bucket,
        endpoint_url=endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    store.ensure_bucket()
    _store = store
    return _store


def set_store(store: ObjectStore | None) -> None:
    """Override the process store. For tests and for explicit wiring."""
    global _store  # noqa: PLW0603
    _store = store
