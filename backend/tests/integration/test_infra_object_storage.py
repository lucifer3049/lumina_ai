"""驗收：MinIO 是否照 12 §4.1 / 11 §4.2 / 05 §3.2 的規格起來。

用 boto3（S3 API）而不是 minio SDK：11 §4.2 的演進路徑是「MinIO → 雲物件儲存
（S3 相容，零程式改動）」——測試也走 S3 API，才算真的驗證了那條路徑。

四件事非測不可：

1. **bucket 存在**：`make up` 之後就該能用，不是等第一次上傳才 404。
2. **versioning 開啟**：12 §4.1 備份策略寫的是「版本化 bucket + 異地 rclone 同步」。
   versioning 只能在 bucket 建立後開啟且會影響刪除語意，事後補開等於前面所有
   物件沒有保護，屬於「不可後補」類。
3. **匿名讀取被拒**：文件內容是租戶資料，bucket 政策若是 public，
   租戶隔離（鐵則 4）在物件儲存這一側直接歸零。
4. **應用憑證不是 root**：應用只需要在單一 bucket 讀寫物件。給 root 憑證的話，
   應用（或任何拿到那組金鑰的人）還能刪掉整個 bucket、關掉 versioning——
   而 versioning 正是第 2 點那條備份策略的地基。

第 4 點的驗收方式是**反向**的：拿應用憑證去做那些不該做的事，斷言被拒。
policy 寫錯時沒有任何症狀，一切照常運作，只是防線不存在——所以非測不可。
"""

from __future__ import annotations

import os
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


# ── 應用憑證是最小權限的 service account，不是 root ──────────────


def _assert_access_denied(exc: botocore.exceptions.ClientError, action: str) -> None:
    """必須是**權限**被拒，不能是其他錯誤剛好也讓操作沒成功。

    例如刪 bucket：policy 錯誤放行時，若 bucket 恰好非空，MinIO 會回 BucketNotEmpty。
    那也是「沒刪成功」，但防線其實是破的——只斷言「有拋例外」會漏掉這種情況。
    """
    code = exc.response["Error"]["Code"]
    assert code in {"AccessDenied", "403"}, (
        f"{action} 的失敗原因是 {code}，不是 AccessDenied——"
        "表示應用憑證其實有這個權限，只是這次剛好沒成功"
    )


def test_application_credentials_are_not_minio_root() -> None:
    """應用的 S3 金鑰不得等於 MinIO root 憑證。

    最容易發生的退化不是 policy 寫錯，而是有人為了省事把 .env 的兩組值設成一樣，
    此時下面幾條反向測試全部照樣通過（root 也會被 policy 擋？不會——root 不受 policy 限制）。
    """
    root_user = os.environ.get("MINIO_ROOT_USER")
    assert root_user, "環境未提供 MINIO_ROOT_USER——應用與 root 憑證應是兩組獨立的值"

    assert get_app_settings().s3_access_key.get_secret_value() != root_user, (
        "應用的 S3_ACCESS_KEY 就是 MINIO_ROOT_USER——應用因此能刪 bucket、關 versioning"
    )


def test_application_cannot_delete_the_bucket(s3) -> None:  # type: ignore[no-untyped-def]
    """刪 bucket 必須被拒（12 §4.1：bucket 一旦刪除，版本歷史一併消失）。

    先放一個 canary 物件再刪：萬一 policy 是錯的，非空 bucket 也刪不掉，
    測試會以 BucketNotEmpty 紅燈而不是真的把開發環境清掉。
    """
    bucket = get_app_settings().s3_bucket
    key = f"tenant-{uuid.uuid4()}/acceptance/canary.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=b"canary")

    try:
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3.delete_bucket(Bucket=bucket)
        _assert_access_denied(exc.value, "刪除 bucket")
    finally:
        s3.delete_object(Bucket=bucket, Key=key)


def test_application_cannot_disable_versioning(s3) -> None:  # type: ignore[no-untyped-def]
    """關閉 versioning 必須被拒——關掉之後所有刪除都變成不可回復。"""
    bucket = get_app_settings().s3_bucket

    try:
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Suspended"},
            )
        _assert_access_denied(exc.value, "關閉 versioning")
    finally:
        # policy 錯誤而真的被關掉時，把環境復原——否則這條紅燈會連帶
        # 汙染後續每一次測試執行（versioning 不會自己回來）。
        if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
            s3.put_bucket_versioning(
                Bucket=bucket,
                VersioningConfiguration={"Status": "Enabled"},
            )


def test_application_cannot_create_another_bucket(s3) -> None:  # type: ignore[no-untyped-def]
    """policy 的範圍是單一 bucket。

    能自建 bucket 就等於能建一個不受 versioning、不受匿名存取設定管的空間，
    前面三條驗收在那個新 bucket 上全部不成立。
    """
    rogue = f"rogue-{uuid.uuid4().hex[:12]}"

    try:
        with pytest.raises(botocore.exceptions.ClientError) as exc:
            s3.create_bucket(Bucket=rogue)
        _assert_access_denied(exc.value, "建立其他 bucket")
    finally:
        # policy 錯誤而真的建起來時要收乾淨，否則失敗一次就留一個孤兒 bucket。
        if rogue in {b["Name"] for b in s3.list_buckets()["Buckets"]}:
            s3.delete_bucket(Bucket=rogue)
