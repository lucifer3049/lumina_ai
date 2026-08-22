"""物件儲存 —— 全 repo 唯一的 S3/MinIO 入口。

與 `core/redis.py` 同一個角色與同一條理由：**租戶前綴強制在這一層**（鐵則 4）。
key 一律 ``tenant-{tenant_id}/kb/{kb_id}/{doc_id}``；前綴若留給呼叫端自己記得加，
遲早有人漏掉，而漏掉的後果是兩個租戶的檔案落在同一個 prefix 底下——日後任何
「以 prefix 掃描」的清理、匯出、配額計算都會跨租戶，而且沒有任何錯誤訊息。

**「強制」是真的擋，不是靠約定**：:func:`put_object` / :func:`get_object` /
:func:`delete_object` 每一次都比對 key 的前綴與當前 TenantContext，不符即 raise
（:class:`~core.exceptions.CrossTenantObjectKeyError`）。只提供 :func:`build_document_key`
是不夠的——它是「請呼叫端自願使用」的函式，而防線不能建立在自願上：任何一個
自己組 key 的呼叫端、或一個被寫壞的 ``documents.storage_key``，都能讀寫別的租戶的
物件。這一點比 Redis 那側更要緊，因為 Redis 的 key 每次現組，物件 key 卻會**持久化
在 DB 裡**——寫壞一次就一直錯下去。唯一豁免的是 :func:`list_keys`（測試／維運通道，
理由見該函式）。

**走 S3 API（boto3）而不是 minio SDK**：11 §4.2 的演進路徑是「MinIO → 雲物件儲存
（S3 相容，零程式改動）」，用 S3 client 那條路才真的走得通。

**同步 client**：與 Redis 同理，呼叫發生在 `run_orm` 的 threadpool 執行緒上
（ADR-001），不會阻塞 event loop。

**key 用 tenant_id 而不是 05 §3.2 寫的 slug**（1B-3 的刻意偏離）：slug 可變（租戶
改名），而 key 一旦寫進 ``documents.storage_key`` 就不能再變。用 slug 的話，改名之後
所有既有物件的 key 都對不上，症狀是「舊文件突然打不開」，而且沒有辦法從 DB 反推
出當時的 slug。
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

import boto3
import botocore.config
import botocore.exceptions

from config.settings.app_settings import get_app_settings
from core.exceptions import CrossTenantObjectKeyError, ObjectNotFoundError
from core.tenant import get_current_tenant_id


@lru_cache(maxsize=1)
def get_s3_client() -> Any:
    """取共用 client（boto3 沒有公開型別，故 ``Any``）。

    ``connect_timeout`` / ``read_timeout`` 出自 11 §4.1（MinIO 30s）：所有對外呼叫
    必有 timeout，否則物件儲存卡住時 threadpool 會被慢慢佔滿，而症狀是「整個網站
    變慢」而不是「物件儲存有問題」。

    ``retries max_attempts=0``：retry 僅限冪等操作（CLAUDE.md）。PUT 在這裡是冪等的
    （key 由 uuid 決定），但預設的自動重試會把逾時的失敗放大成多份流量，且掩蓋
    「這次呼叫其實很慢」這個訊號。需要重試的地方由呼叫端顯式決定。
    """
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
            retries={"max_attempts": 0},
        ),
    )


def warm_up() -> None:
    """先把 client 建好，別讓第一個上傳請求付這筆成本。

    boto3 建 client 要載入 botocore 的服務描述檔（數百個 JSON）。在本機開發的
    WSL2 + 掛載磁碟上實測 **15.6 秒**（容器內約 1 秒）——而那全部落在重啟後的
    第一個上傳請求上。症狀是「上傳偶爾非常慢」，且只在剛部署完出現，看起來像
    物件儲存有問題。

    純本地作業（不連線），因此不會因為 MinIO 沒起來而失敗。
    """
    get_s3_client()


def _tenant_prefix(*, operation: str) -> str:
    """當前租戶的 key 前綴（``tenant-{tenant_id}/``）。

    缺 TenantContext 一律 raise（Fail Fast）：沒有租戶就沒有前綴可比，而「比不了
    就放行」正是這一層要消滅的東西。
    """
    return f"tenant-{get_current_tenant_id(operation=operation)}/"


def _require_own_key(key: str, *, operation: str) -> None:
    """key 必須屬於當前租戶，否則 raise。

    **每一次讀寫都比對，不是只在組 key 的時候**：key 會被存進 ``documents.storage_key``
    再拿回來用，而中間隔著一次 DB 往返、一次 Celery 任務參數、以及未來任何一個
    自己拼 key 的呼叫端。前綴只在組的時候正確，等於防線只擋得住從沒出過錯的路徑。

    比對用 ``startswith`` 而非相等：同一個租戶底下還有 kb／版本／中間產物等層級
    （見 `services/knowledge/ingestion.py` 的 cleaned.json）。前綴尾端的 ``/`` 不能
    省——少了它，``tenant-1...`` 會通過 ``tenant-1`` 開頭的比對，而 UUID 沒有規定
    誰不能是誰的前綴。
    """
    prefix = _tenant_prefix(operation=operation)
    if not key.startswith(prefix):
        raise CrossTenantObjectKeyError(operation=operation, expected_prefix=prefix)


def build_document_key(*, kb_id: uuid.UUID, document_id: uuid.UUID) -> str:
    """組出文件的 object key。

    缺 TenantContext 一律 raise（Fail Fast）：回一個沒有前綴的 key 會讓檔案落在
    bucket 根目錄——不屬於任何租戶、不會被任何清理流程掃到，而且完全沒有錯誤。
    """
    return f"{_tenant_prefix(operation='build_document_key')}kb/{kb_id}/{document_id}"


def put_object(key: str, content: bytes, *, content_type: str) -> None:
    _require_own_key(key, operation="put_object")
    get_s3_client().put_object(
        Bucket=get_app_settings().s3_bucket,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


def get_object(key: str) -> bytes:
    """讀回物件；不存在時 raise :class:`ObjectNotFoundError`。

    把 botocore 的 ``ClientError`` 轉成自家例外是必要的：原始例外會一路冒到
    `api/main.py` 的兜底 handler 變成 500，而它的訊息帶著 bucket 名稱與端點
    ——那是內部拓撲（鐵則 9）。
    """
    _require_own_key(key, operation="get_object")
    try:
        response = get_s3_client().get_object(Bucket=get_app_settings().s3_bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise ObjectNotFoundError("物件不存在") from exc
        raise
    body: bytes = response["Body"].read()
    return body


def delete_object(key: str) -> None:
    """刪除物件。**冪等**——刪一個不存在的 key 不會失敗。

    S3 的 DeleteObject 本身對不存在的 key 就回成功，這裡不另外檢查：清理流程會重跑
    （08 §6 的冪等原則），而「先查再刪」除了多一次往返之外，中間還有競態。
    """
    _require_own_key(key, operation="delete_object")
    get_s3_client().delete_object(Bucket=get_app_settings().s3_bucket, Key=key)


def list_keys(prefix: str = "") -> list[str]:
    """列出 bucket 內的 key。

    **只供測試與維運腳本使用**，正式路徑不該有這種呼叫：它會掃過整個 prefix，
    成本隨物件數成長，而應用需要的資訊（某個租戶有哪些文件）在 DB 裡查得更快也
    更準（DB 才有軟刪除與狀態）。留在這一層而不是讓測試自己建 client，是為了不讓
    連線設定（endpoint、憑證、timeout）出現第二份。

    **這是唯一豁免租戶前綴檢查的函式**（其餘三個一律比對，見 `_require_own_key`）：
    它的用途本來就是「跨租戶看整個 bucket」——維運要確認孤兒物件、測試要確認某個
    租戶的前綴底下真的沒有別人的東西，而那兩件事都必須看得見不屬於當前租戶的 key。
    正式路徑沒有它的呼叫點，所以豁免不會變成繞道。
    """
    paginator = get_s3_client().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=get_app_settings().s3_bucket, Prefix=prefix):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return keys
