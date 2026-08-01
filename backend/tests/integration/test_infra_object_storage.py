"""驗收：MinIO 是否照 12 §4.1 / 11 §4.2 / 05 §3.2 的規格起來。

用 boto3（S3 API）而不是 minio SDK：11 §4.2 的演進路徑是「MinIO → 雲物件儲存
（S3 相容，零程式改動）」——測試也走 S3 API，才算真的驗證了那條路徑。

三件事非測不可：

1. **bucket 存在**：`make up` 之後就該能用，不是等第一次上傳才 404。
2. **versioning 開啟**：12 §4.1 備份策略寫的是「版本化 bucket + 異地 rclone 同步」。
   versioning 只能在 bucket 建立後開啟且會影響刪除語意，事後補開等於前面所有
   物件沒有保護，屬於「不可後補」類。
3. **匿名讀取被拒**：文件內容是租戶資料，bucket 政策若是 public，
   租戶隔離（鐵則 4）在物件儲存這一側直接歸零。
"""

from __future__ import annotations

import uuid

import boto3
import botocore.config
import botocore.exceptions
import pytest
from botocore import UNSIGNED

from config.settings.app_settings import get_app_settings

# 11 §4.1 Timeout 全域字典：MinIO 30s。
S3_TIMEOUT_SECONDS = 30


@pytest.fixture
def s3():  # type: ignore[no-untyped-def]  # boto3 client 無公開型別
    settings = get_app_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        config=botocore.config.Config(
            signature_version="s3v4",
            connect_timeout=settings.s3_timeout_seconds,
            read_timeout=settings.s3_timeout_seconds,
            retries={"max_attempts": 0},  # retry 僅限冪等操作，測試不吃重試遮蔽
        ),
    )


def test_bucket_exists(s3) -> None:  # type: ignore[no-untyped-def]
    bucket = get_app_settings().s3_bucket
    names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket in names, f"bucket {bucket} 不存在——`make up` 應冪等建立它"


def test_bucket_versioning_enabled(s3) -> None:  # type: ignore[no-untyped-def]
    """版本化 bucket（12 §4.1）——事後補開不保護既有物件，故列驗收項。"""
    status = s3.get_bucket_versioning(Bucket=get_app_settings().s3_bucket).get("Status")
    assert status == "Enabled", f"versioning 狀態是 {status}，規格是 Enabled（12 §4.1）"


def test_object_roundtrip_under_tenant_prefix(s3) -> None:  # type: ignore[no-untyped-def]
    """put → get → delete 走得通，key 使用 `tenant-{id}/` 前綴（01 §3、05 §3.2）。"""
    bucket = get_app_settings().s3_bucket
    key = f"tenant-{uuid.uuid4()}/acceptance/{uuid.uuid4()}.txt"
    body = "驗收物件".encode()

    s3.put_object(Bucket=bucket, Key=key, Body=body)
    try:
        fetched = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert fetched == body
    finally:
        s3.delete_object(Bucket=bucket, Key=key)


def test_anonymous_access_denied(s3) -> None:  # type: ignore[no-untyped-def]
    """未簽章的請求必須被拒——bucket 不可為 public（鐵則 4：租戶隔離）。"""
    settings = get_app_settings()
    bucket = settings.s3_bucket
    key = f"tenant-{uuid.uuid4()}/acceptance/{uuid.uuid4()}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"secret")

    anonymous = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        config=botocore.config.Config(
            signature_version=UNSIGNED,
            connect_timeout=settings.s3_timeout_seconds,
            read_timeout=settings.s3_timeout_seconds,
            retries={"max_attempts": 0},
        ),
    )

    try:
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            anonymous.get_object(Bucket=bucket, Key=key)
        assert exc.value.response["Error"]["Code"] in {"AccessDenied", "403"}
    finally:
        s3.delete_object(Bucket=bucket, Key=key)


def test_client_timeout_matches_spec() -> None:
    """宣告值對帳：11 §4.1 全域字典 MinIO 30s。"""
    assert get_app_settings().s3_timeout_seconds == S3_TIMEOUT_SECONDS
