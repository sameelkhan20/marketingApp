"""Cloudflare R2 (S3 API), same bucket settings as the Social Agent. Higgsfield imports reference
pictures by public https URL, so the product photo is put here for the length of one job."""

import secrets

from .config import R2_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET, R2_PUBLIC_URL, R2_SECRET_KEY


def configured() -> bool:
    return bool(R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY and R2_BUCKET and R2_PUBLIC_URL)


def _client():
    import boto3  # lazy: only needed when Higgsfield gets a reference picture
    return boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def put_temp(data: bytes, mime: str) -> tuple[str, str]:
    """(key, public url)."""
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    key = f"ads-planner-refs/{secrets.token_hex(12)}.{ext}"
    _client().put_object(Bucket=R2_BUCKET, Key=key, Body=data, ContentType=mime)
    return key, f"{R2_PUBLIC_URL.rstrip('/')}/{key}"


def delete(key: str) -> None:
    _client().delete_object(Bucket=R2_BUCKET, Key=key)
