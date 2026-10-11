"""Archives order snapshots to S3."""
from __future__ import annotations

import json

import boto3

ARCHIVE_BUCKET = "acme-order-archive"


class S3OrderArchive:
    def __init__(self, bucket: str = ARCHIVE_BUCKET, client=None) -> None:
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    def archive(self, snapshot: dict) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=f"orders/{snapshot['order_id']}.json",
            Body=json.dumps(snapshot).encode("utf-8"),
            ContentType="application/json",
        )
