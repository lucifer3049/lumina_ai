"""NotificationService —— 通道抽象、事件訂閱規則、去重與節流（04 §8.5，2A-5）。

2A 之前，「文件失敗了」「額度快滿了」「文件可以問了」這三件事都只寫在 DB 裡，
沒有任何一條路徑會主動告訴使用者——13 §3 的三張結案表各記了一次「通知排 2A」。
這個模組是那三筆的落點。

三個設計決定：

1. **旁路原則**（同 `AuditService.record`、`UsageService`）：`notify_*` 全部吞例外。
   通知寄不出去只能失去這一則；反過來會讓「上傳」或「額度檢查」因為通知系統的
   問題而失敗，而那是使用者真正在做的事。
2. **email 一律經 Celery**。SMTP 是外部系統，連不上時要等到 timeout——那筆時間
   不能落在使用者按下的那個按鈕上。這裡只送一個 id 進佇列。
3. **收件人由事件決定，不是廣播**。文件事件寄給上傳者（`documents.uploaded_by`，
   2A-5 新增；NULL 的舊文件退回 owner／admin）；quota 是管理面資訊，只寄
   owner／admin——界線同 `analytics:read`。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from common.cursors import CursorError, decode_cursor, encode_cursor
from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import NotFoundError, ValidationFailedError
from core.tasks import enqueue_notification_email
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import UserRepository
from repositories.knowledge import DocumentRepository
from repositories.platform import NotificationRepository

logger = get_logger(__name__)

__all__ = [
    "CHANNELS_BY_TYPE",
    "CHANNEL_EMAIL",
    "CHANNEL_IN_APP",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "TYPE_DOCUMENT_FAILED",
    "TYPE_DOCUMENT_READY",
    "TYPE_EVALUATION_COMPLETED",
    "TYPE_QUOTA_THRESHOLD",
    "InboxPage",
    "NotificationRecord",
    "NotificationService",
    "channels_for",
    "collapse_bucket",
    "quota_thresholds",
]

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"

TYPE_DOCUMENT_FAILED = "document.failed"
TYPE_DOCUMENT_READY = "document.ready"
TYPE_QUOTA_THRESHOLD = "quota.threshold"
TYPE_EVALUATION_COMPLETED = "evaluation.completed"

# 事件訂閱規則（04 §8.5）。**新事件型別必須列進來**：查不到就退回空 tuple 的話，
# 新加的事件會安靜地誰都不通知，症狀與「還沒接線」一模一樣
# （tests/unit/test_notification_channels.py 手寫一份清單守門）。
#
# 分野是「這件事需不需要離開這個網站」：失敗要有人去修、額度爆了要有人去加，
# 而使用者不會為了看有沒有壞消息定期登入。ready 是好消息且量大（一次上傳一批
# 就是一批），寄信會變成騷擾。
CHANNELS_BY_TYPE: dict[str, tuple[str, ...]] = {
    TYPE_DOCUMENT_FAILED: (CHANNEL_IN_APP, CHANNEL_EMAIL),
    TYPE_QUOTA_THRESHOLD: (CHANNEL_IN_APP, CHANNEL_EMAIL),
    TYPE_DOCUMENT_READY: (CHANNEL_IN_APP,),
    # Evaluation 模組排 Phase 3——型別與訂閱規則先就位，現在沒有產生者。
    TYPE_EVALUATION_COMPLETED: (CHANNEL_IN_APP,),
}

# quota 告警的收件人：用量輪廓是管理面資訊（界線同 `analytics:read`）。
_QUOTA_ROLES = ("owner", "admin")

_RESOURCE_LABELS = {
    "tokens_month": "本月 token",
    "messages_day": "今日訊息數",
    "documents": "文件數",
    "storage_bytes": "儲存空間",
    "streams": "同時串流數",
}


def channels_for(event_type: str) -> tuple[str, ...]:
    """這個事件實際會走的通道（訂閱規則 ∩ 目前開著的通道）。

    email 關掉時**只失去 email**：沒有設定 SMTP 的環境（CI）不該連站內收件匣
    一起失去，通知的最低保證是「站內看得到」。
    """
    declared = CHANNELS_BY_TYPE.get(event_type, (CHANNEL_IN_APP,))
    if get_app_settings().notification_email_enabled:
        return declared
    return tuple(channel for channel in declared if channel != CHANNEL_EMAIL)


def quota_thresholds() -> tuple[int, ...]:
    """告警門檻（百分比，由小到大）。壞條目只失去那一個門檻——形狀同價目表與
    quota plan 的解析：一個打錯的值不該讓整組告警消失。"""
    values: list[int] = []
    for entry in get_app_settings().notification_quota_thresholds.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        try:
            value = int(entry)
        except ValueError:
            logger.warning("quota_threshold_entry_ignored", entry=entry)
            continue
        if 0 < value <= 100:
            values.append(value)
    return tuple(sorted(set(values)))


def collapse_bucket(kb_id: uuid.UUID, *, at: datetime | None = None) -> str:
    """「文件已完成」的收合鍵：同一個 KB、同一個時間桶 → 同一列。

    **桶是對齊過的絕對時間，不是「距離上一則多久」**：後者要先查最後一則、再比
    時間差，而兩個 worker 同時完成兩份文件時會各自查到「還沒有」。對齊的桶讓
    去重完全落在 DB 的唯一約束上。

    邊界是 KB：合過頭的話，「法規」與「新人訓練」會變成一句「2 份文件已完成」，
    而使用者點進去的是錯的地方。
    """
    window = max(get_app_settings().notification_collapse_window_minutes, 1)
    moment = at or datetime.now(UTC)
    slot = int(moment.timestamp()) // (window * 60)
    return f"{TYPE_DOCUMENT_READY}:{kb_id}:{slot}"


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """一則通知（對外形狀）。"""

    id: uuid.UUID
    type: str
    title: str
    body: str
    channels: list[str]
    meta: dict[str, Any]
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: list[NotificationRecord]
    next_cursor: str | None
    unread_count: int


def _decode(cursor: str | None) -> dict[str, Any] | None:
    """游標字串 → dict；壞掉是 422 而不是「當成第一頁」（理由見 common/cursors.py）。"""
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as exc:
        raise ValidationFailedError("游標不正確") from exc


class NotificationService:
    def __init__(
        self,
        *,
        notifications: NotificationRepository | None = None,
        users: UserRepository | None = None,
        documents: DocumentRepository | None = None,
    ) -> None:
        self._notifications = notifications or NotificationRepository()
        self._users = users or UserRepository()
        self._documents = documents or DocumentRepository()

    # ── 收件匣（09 §2.6）────────────────────────────────────

    def inbox(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        unread_only: bool = False,
    ) -> InboxPage:
        """這個人的收件匣＋未讀數。

        未讀數與清單在**同一個交易**裡算：分開查的話，兩次查詢之間進來的新通知
        會讓鈴鐺上的數字與點開之後看到的內容對不起來。
        """
        try:
            with tenant_context(tenant_id), unit_of_work():
                rows, next_key = self._notifications.inbox(
                    user_id=user_id,
                    limit=limit,
                    cursor=_decode(cursor),
                    unread_only=unread_only,
                )
                unread = self._notifications.unread_count(user_id)
        except CursorError as exc:
            raise ValidationFailedError("游標不正確") from exc
        return InboxPage(
            items=[
                NotificationRecord(
                    id=uuid.UUID(str(row.id)),
                    type=row.type,
                    title=row.title,
                    body=row.body,
                    channels=list(row.channels),
                    meta=dict(row.meta),
                    read_at=row.read_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ],
            next_cursor=encode_cursor(next_key) if next_key else None,
            unread_count=unread,
        )

    def mark_read(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> NotificationRecord:
        """標為已讀並回傳那一列。

        **別人的（或不存在的）一律 `NotFoundError` → 404**：403 等於承認這個 id
        存在，而通知 id 猜得到——連續請求就能數出別人收到幾則、什麼時候收到。
        """
        with tenant_context(tenant_id), unit_of_work():
            if not self._notifications.mark_read(user_id=user_id, notification_id=notification_id):
                raise NotFoundError("通知不存在")
            row = self._notifications.get_by_id(notification_id)
            if row is None:  # pragma: no cover —— 同一個交易內剛確認過存在
                raise NotFoundError("通知不存在")
            return NotificationRecord(
                id=uuid.UUID(str(row.id)),
                type=row.type,
                title=row.title,
                body=row.body,
                channels=list(row.channels),
                meta=dict(row.meta),
                read_at=row.read_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    # ── 事件 ────────────────────────────────────────────────

    def notify_document_failed(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID, *, error: dict[str, object]
    ) -> None:
        """文件進了 DLQ 或是毒檔（08 §6）。**永遠不拋例外**（旁路原則）。

        `error` 直接來自 `document.error`——那份 dict 已經過 `error_payload` 的
        過濾（基礎設施錯誤只給型別名、不給原文）。**不要在這裡自己去讀 exception**，
        那等於把 1B 立的那道牆繞過去，而通知離使用者最近。
        """
        pending: list[uuid.UUID] = []
        try:
            with tenant_context(tenant_id), unit_of_work():
                document = self._documents.get_by_id(document_id)
                if document is None:
                    return
                retryable = bool(error.get("retryable"))
                stage = str(error.get("stage", ""))
                pending = self._deliver(
                    recipients=self._recipients_for(document.uploaded_by),
                    event_type=TYPE_DOCUMENT_FAILED,
                    title=f"{document.filename} 處理失敗",
                    body=(
                        "這份文件在處理過程中失敗了，可以在知識庫裡重跑。"
                        if retryable
                        else "這份文件無法解析，換一個格式或重新匯出後再上傳。"
                    ),
                    meta={
                        "document_id": str(document_id),
                        "kb_id": str(document.kb_id),
                        "filename": document.filename,
                        "stage": stage,
                        "retryable": retryable,
                        "cause": str(error.get("cause", "")),
                    },
                    tenant_id=tenant_id,
                )
        except Exception:
            logger.exception(
                "notify_failed", kind=TYPE_DOCUMENT_FAILED, document_id=str(document_id)
            )
            return
        self._flush_emails(tenant_id, pending)

    def notify_document_ready(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        """文件走到 `ready`——「可以問了」的那一刻（08 §2 的狀態機終點）。

        同一個 KB、同一個時間桶內的多份合成一列（04 §8.5 的節流）：一次上傳 50 份
        就是 50 則，而那會把收件匣洗掉。
        """
        pending: list[uuid.UUID] = []
        try:
            with tenant_context(tenant_id), unit_of_work():
                document = self._documents.get_by_id(document_id)
                if document is None:
                    return
                key = collapse_bucket(uuid.UUID(str(document.kb_id)))
                for user_id in self._recipients_for(document.uploaded_by):
                    pending += self._deliver_ready(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        dedupe_key=key,
                        filename=document.filename,
                        kb_id=uuid.UUID(str(document.kb_id)),
                        document_id=document_id,
                    )
        except Exception:
            logger.exception(
                "notify_failed", kind=TYPE_DOCUMENT_READY, document_id=str(document_id)
            )
            return
        self._flush_emails(tenant_id, pending)

    def notify_quota_threshold(
        self, tenant_id: uuid.UUID, resource: str, *, used: int, limit: int, period_start: str
    ) -> None:
        """用量跨過門檻（04 §8.5 的 80%／100%）。

        去重鍵含**期別**：新的一期自然是新的鍵，不需要「清掉上一期的告警」這種
        會忘記跑的東西（形狀同 quota 計數器的期別 key，2A-2a）。
        """
        if limit <= 0:
            return
        percent = used * 100 // limit
        pending: list[uuid.UUID] = []
        try:
            with tenant_context(tenant_id), unit_of_work():
                recipients = [
                    uuid.UUID(str(user.id)) for user in self._users.active_with_roles(_QUOTA_ROLES)
                ]
                label = _RESOURCE_LABELS.get(resource, resource)
                for threshold in quota_thresholds():
                    if percent < threshold:
                        continue
                    pending += self._deliver(
                        recipients=recipients,
                        event_type=TYPE_QUOTA_THRESHOLD,
                        title=(
                            f"{label}已用盡" if threshold >= 100 else f"{label}已用掉 {threshold}%"
                        ),
                        body=(
                            f"目前用量 {used} / {limit}。"
                            + ("超過的請求會被擋下。" if threshold >= 100 else "")
                        ),
                        meta={
                            "resource": resource,
                            "threshold": threshold,
                            "used": used,
                            "limit": limit,
                        },
                        tenant_id=tenant_id,
                        dedupe_key=f"{TYPE_QUOTA_THRESHOLD}:{resource}:{period_start}:{threshold}",
                    )
        except Exception:
            logger.exception("notify_failed", kind=TYPE_QUOTA_THRESHOLD, resource=resource)
            return
        self._flush_emails(tenant_id, pending)

    def send_email(self, tenant_id: uuid.UUID, notification_id: uuid.UUID) -> bool:
        """把一則已經寫下的通知寄成 email（由 worker 呼叫）。

        **這裡不吞例外**：與 `notify_*` 相反——寄信失敗要讓 task 決定重試，而
        `notify_*` 的呼叫端是使用者的請求路徑。找不到通知（已被清理）回 False，
        那不是錯誤，重試也不會變好。
        """
        from core.mailer import send_email as deliver

        with tenant_context(tenant_id), unit_of_work():
            row = self._notifications.get_by_id(notification_id)
            if row is None:
                return False
            user = self._users.get_by_id(uuid.UUID(str(row.user_id)))
            if user is None or not user.email:
                return False
            recipient, subject, body = str(user.email), row.title, row.body

        # 寄信在交易外：SMTP 是外部系統，把它包在 DB 交易裡等於讓一條連線陪著
        # 它一起等 timeout（而那條連線來自連線池）。
        deliver(to=[recipient], subject=subject, body=body)
        return True

    # ── 派送 ────────────────────────────────────────────────

    def _deliver(
        self,
        *,
        recipients: list[uuid.UUID],
        event_type: str,
        title: str,
        body: str,
        meta: dict[str, object],
        tenant_id: uuid.UUID,
        dedupe_key: str | None = None,
    ) -> list[uuid.UUID]:
        """寫 in-app 那幾列；回傳其中**還要寄信**的 id。

        去重的那些（`dedupe_key` 不為 None）**先查再插**只是省一次撞牆——真正的
        保證是唯一約束，因此撞到了就當成「已經有人通知過」，不是錯誤。這句話由
        `NotificationRepository.create_if_absent` 的 savepoint 實作：沒有它的話，
        `IntegrityError` 會讓整個交易進 aborted 狀態，**同一批裡已經寫好的其他收件人
        一起回滾**，而外層的 `except Exception` 把它整個吞掉——症狀是兩個 worker 同時
        完成時，其中一批通知整個消失。

        **寄信不在這裡做**：呼叫端還在交易裡，而 worker 可能在 COMMIT 之前就拿到
        訊息、查不到那一列（症狀是「偶爾沒收到信」，重試也救不回來——那時它已經
        存在了）。同一個教訓寫在 `DocumentService.upload` 的 enqueue 註解裡。
        """
        channels = list(channels_for(event_type))
        pending: list[uuid.UUID] = []
        for user_id in recipients:
            row: Any
            if dedupe_key is None:
                row = self._notifications.create(
                    user_id=user_id,
                    type=event_type,
                    title=title,
                    body=body,
                    meta=meta,
                    channels=channels,
                )
            else:
                if self._notifications.get_by_dedupe(user_id=user_id, dedupe_key=dedupe_key):
                    continue
                row = self._notifications.create_if_absent(
                    user_id=user_id,
                    type=event_type,
                    title=title,
                    body=body,
                    meta=meta,
                    channels=channels,
                    dedupe_key=dedupe_key,
                )
                if row is None:
                    # 別人在這兩步之間插進去了——那正是「已經有人通知過」。
                    continue
            if CHANNEL_EMAIL in channels:
                pending.append(uuid.UUID(str(row.id)))
        return pending

    def _deliver_ready(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        dedupe_key: str,
        filename: str,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """ready 的收合：桶裡已經有一列就把它加一，否則開新的一列。

        回傳「還要寄信的 id」（理由同 `_deliver`：交易還沒 COMMIT）。

        **兩個 worker 同時完成同一桶的兩份文件是常態**（一次上傳一批，embed 是平行
        的），所以這裡的兩條路徑都要能承受併發：

        - 都判定「桶是空的」→ 一邊插成功、一邊撞唯一約束。撞到的那邊改走收合，
          而不是讓整個交易死掉。
        - 都判定「桶裡有一列」→ 收合是 read-modify-write（讀 meta、count+1、寫回），
          兩邊都讀到 1 的話結果是「1 份已完成」而不是 2。所以取既有那列時鎖住它，
          後到的那個等前一個寫完再讀。
        """
        existing = self._notifications.get_by_dedupe(
            user_id=user_id, dedupe_key=dedupe_key, for_update=True
        )
        if existing is None:
            row = self._notifications.create_if_absent(
                user_id=user_id,
                type=TYPE_DOCUMENT_READY,
                title=f"{filename} 已完成",
                body="可以開始問答了。",
                meta={
                    "kb_id": str(kb_id),
                    "count": 1,
                    "document_ids": [str(document_id)],
                    "filename": filename,
                },
                channels=list(channels_for(TYPE_DOCUMENT_READY)),
                dedupe_key=dedupe_key,
            )
            if row is not None:
                return [uuid.UUID(str(row.id))] if CHANNEL_EMAIL in row.channels else []
            # 這一瞬間有人先開了這個桶——回頭走收合那條路（鎖著讀，這次一定拿得到）。
            existing = self._notifications.get_by_dedupe(
                user_id=user_id, dedupe_key=dedupe_key, for_update=True
            )
            if existing is None:
                # 插不進去又讀不到：只可能是同一瞬間被清理掉。少一則通知，不值得
                # 讓「文件完成」這件事失敗。
                return []

        meta = dict(existing.meta)
        count = int(meta.get("count", 1)) + 1
        # 只留前 10 個 id：前端要的是「點進去看哪一批」，而 meta 會隨每一次收合
        # 被整個重寫——不設上限的話，一次匯入幾百份就是一列幾百個 UUID。
        ids = [*list(meta.get("document_ids", []))[:9], str(document_id)]
        meta.update({"count": count, "document_ids": ids})
        self._notifications.collapse(
            uuid.UUID(str(existing.id)),
            title=f"{count} 份文件已完成",
            body=f"{filename} 等 {count} 份文件可以開始問答了。",
            meta=meta,
        )
        # 收合不重寄信：同一批的第二封只會是同一句話再說一次。
        return []

    def _flush_emails(self, tenant_id: uuid.UUID, notification_ids: list[uuid.UUID]) -> None:
        """**交易提交之後**才把信丟進佇列（理由見 `_deliver`）。

        送不進去也不影響已經寫好的 in-app 那一列——站內看得到是最低保證，
        email 只是「順便送到他眼前」。
        """
        for notification_id in notification_ids:
            try:
                enqueue_notification_email(tenant_id=tenant_id, notification_id=notification_id)
            except Exception:
                logger.warning(
                    "notification_email_enqueue_failed",
                    notification_id=str(notification_id),
                    exc_info=True,
                )

    def _recipients_for(self, uploaded_by: uuid.UUID | None) -> list[uuid.UUID]:
        """文件事件的收件人：上傳者，不知道就退回 owner／admin。

        **不能是空的**：DLQ 的意義就是有人去修，而「沒有人收到」與「沒有接線」
        是同一個結果。舊文件的 `uploaded_by` 一律是 NULL（2A-5 才加的欄位）。
        """
        if uploaded_by is not None:
            return [uploaded_by]
        return [uuid.UUID(str(user.id)) for user in self._users.active_with_roles(_QUOTA_ROLES)]
