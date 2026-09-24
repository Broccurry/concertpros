"""Backblaze B2 file storage, via its S3-compatible API.

Railway's filesystem is wiped on every deploy, so uploaded files (offers,
contracts, flyers) can't live on local disk — they live in B2, and this app
only ever stores a pointer to them (event_files.storage_key). The app never
proxies file bytes itself: the browser uploads/downloads directly against a
short-lived presigned URL, generated here.

A separate bucket + a bucket-scoped Application Key from ConcertPros' own B2
account — never SellHQ's — per CLAUDE.md's no-shared-credentials rule.
"""
import os
import uuid

import boto3
from botocore.config import Config

UPLOAD_URL_TTL = 600   # seconds — long enough for a slow upload on a phone
DOWNLOAD_URL_TTL = 300


def _client():
    endpoint = os.environ["B2_ENDPOINT"]
    region = endpoint.split(".")[1]  # "s3.us-east-005.backblazeb2.com" -> "us-east-005"
    return boto3.client(
        "s3",
        endpoint_url=f"https://{endpoint}",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4"),
        region_name=region,
    )


def new_storage_key(event_id, filename, venue_id=None, folder_id=None):
    folder = (f"event-{event_id}" if event_id else
              f"venue-{venue_id}" if venue_id else
              f"folder-{folder_id}" if folder_id else "general")
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"{folder}/{uuid.uuid4().hex}-{safe_name}"


def presign_upload(storage_key, content_type):
    return _client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": os.environ["B2_BUCKET"],
            "Key": storage_key,
            "ContentType": content_type or "application/octet-stream",
        },
        ExpiresIn=UPLOAD_URL_TTL,
    )


def presign_download(storage_key, filename, disposition="inline"):
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["B2_BUCKET"],
            "Key": storage_key,
            "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
        },
        ExpiresIn=DOWNLOAD_URL_TTL,
    )


def delete_object(storage_key):
    _client().delete_object(Bucket=os.environ["B2_BUCKET"], Key=storage_key)
