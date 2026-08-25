"""Cloud Storage helpers.

Source PDFs and raw Document AI responses live here; the database keeps only
their URIs. A missing bucket fails loudly rather than writing to container-local
disk, where the files would disappear on the next Cloud Run revision.
"""
from __future__ import annotations

import json
import logging

from ..config import settings

log = logging.getLogger(__name__)


def _bucket():
    from google.cloud import storage

    return storage.Client(project=settings.project_id or None).bucket(settings.bucket)


def upload_bytes(path: str, data: bytes, content_type: str) -> str:
    if not settings.bucket:
        raise RuntimeError("GCS_BUCKET is not set")

    blob = _bucket().blob(path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{settings.bucket}/{path}"


def upload_json(path: str, payload: dict) -> str:
    return upload_bytes(
        path, json.dumps(payload, indent=2, default=str).encode("utf-8"),
        "application/json")


def download_bytes(uri: str) -> bytes:
    if not uri.startswith("gs://"):
        raise RuntimeError(f"not a Cloud Storage URI: {uri!r}")
    _, _, rest = uri.partition("gs://")
    bucket_name, _, blob_path = rest.partition("/")
    from google.cloud import storage

    client = storage.Client(project=settings.project_id or None)
    return client.bucket(bucket_name).blob(blob_path).download_as_bytes()


def signed_url(uri: str, minutes: int = 30) -> str | None:
    """Time-limited link so the dashboard can open the source PDF."""
    if not uri or not uri.startswith("gs://"):
        return None
    from datetime import timedelta

    from google.cloud import storage

    _, _, rest = uri.partition("gs://")
    bucket_name, _, blob_path = rest.partition("/")
    client = storage.Client(project=settings.project_id or None)
    blob = client.bucket(bucket_name).blob(blob_path)
    try:
        return blob.generate_signed_url(
            version="v4", expiration=timedelta(minutes=minutes), method="GET")
    except Exception as exc:  # noqa: BLE001 - signing needs a key, not always present
        log.warning("could not sign url for %s: %s", uri, exc)
        return None
