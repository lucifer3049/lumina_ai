"""Quota——租戶級配額的定義與強制執行（04 §8.1，2A-2a）。

五種資源，三種計量方式（依資源的本性選，不硬套同一套機制）：

- **流量**（`tokens_month`、`messages_day`）：Redis 計數器，key 按期別命名
  （月 `YYYY-MM`、日 `YYYY-MM-DD`）＋ TTL——新的一期自然從 0 開始，**沒有
  重置 job** 這種會漏跑的東西。token 走 reserve/commit：實際用量要到生成結束
  才知道，開場按估計值（`quota_token_reserve_estimate`）預留，結束時校正。
- **存量**（`documents`、`storage_bytes`）：DB 聚合（active 文件的 COUNT / SUM），
  Redis 不參與——存量必須活得比 Redis 久，刪除即釋放、永不漂移。
- **並發**（`streams`）：Redis gauge，開始 +1、結束 −1，帶安全 TTL（行程死掉
  不會永久佔位）。

限額解析：plan 預設（`app_settings.quota_plan_free`，可調）→ 租戶覆寫
（`tenant.settings["quota"]`，2C 設定畫面那層）。缺鍵＝**不限制**：漏列往寬鬆
倒——反向的話，新資源加進系統的那一天，所有租戶同時被鎖死。

超額行為本階段只做**硬擋**（13 §4 的 DoD）；04 §8.1 的「降級模型／記錄後放行」
延後，等有真實租戶的 plan 需要它。

DB 快照（quota_counters）與 usage_logs 日結對帳屬 2A-2b。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from config.logging import get_logger
from config.settings.app_settings import get_app_settings
from core.exceptions import DomainError, ErrorCode
from core.redis import get_redis, tenant_key
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.identity import TenantRepository
from repositories.knowledge import DocumentRepository
from services.platform.notifications import NotificationService, quota_thresholds

logger = get_logger(__name__)

__all__ = [
    "RESOURCES",
    "QuotaExceededError",
    "QuotaReservation",
    "QuotaService",
    "QuotaStatus",
    "resolve_limits",
]

# 名字是 API 契約的一部分（quota 端點與 429 的 details 都帶它）。
RESOURCES = ("tokens_month", "messages_day", "documents", "storage_bytes", "streams")

_PERIOD_MONTH = frozenset({"tokens_month"})
_PERIOD_DAY = frozenset({"messages_day"})
_DB_BACKED = frozenset({"documents", "storage_bytes"})
_GAUGES = frozenset({"streams"})

# gauge 的安全 TTL：正常路徑靠 release 歸還；行程被砍時靠它自然消失。每次
# reserve 都會刷新，所以活著的串流不會被它清掉（單輪生成遠短於一小時）。
_GAUGE_TTL_SECONDS = 3600
# 告警去重的 Redis 擋板壽命。比最長的期別（月）長一點即可——真正的去重在
# DB 的唯一約束上，這只是省下熱路徑的查詢。
_NOTIFY_GUARD_TTL_SECONDS = 40 * 24 * 3600
# 週期 key 在期末之後多留一天：對帳（2A-2b）要讀「剛結束的那一期」。
_PERIOD_GRACE = timedelta(days=1)


class QuotaExceededError(DomainError):
    """429（09 附錄 A：重試無用）。details 是 client 唯一的機器可讀資訊。"""

    code = ErrorCode.QUOTA_EXCEEDED


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    """一次成功的預留——之後用它 commit（校正為實際值）或 release（歸還）。"""

    tenant_id: uuid.UUID
    resource: str
    amount: int
    key: str


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    resource: str
    limit: int | None
    used: int
    remaining: int | None
    resets_at: datetime | None


def _parse_plan(raw: str) -> dict[str, int]:
    """`resource:value;...` → 限額表。壞條目只失去那一條（同價目表的容忍方向）。"""
    limits: dict[str, int] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        name, _, value_part = entry.partition(":")
        try:
            value = int(value_part.strip())
        except ValueError:
            logger.warning("quota_plan_entry_rejected", entry=entry, reason="not_an_int")
            continue
        if value < 0:
            logger.warning("quota_plan_entry_rejected", entry=entry, reason="negative")
            continue
        limits[name.strip()] = value
    return limits


def resolve_limits(
    plan: str,
    overrides: dict[str, Any] | None = None,
    *,
    raw_plan: str | None = None,
) -> dict[str, int | None]:
    """plan 預設 → 租戶覆寫；回傳五種資源的完整表，None＝不限制。

    `raw_plan` 只給測試注入用（直接驗「plan 字串缺鍵」的語意）。plan 名稱目前
    只認 free——打錯字或未知方案拿到的是最保守的一組，不是不設限。
    """
    del plan  # 目前唯一的 plan 是 free；未知一律退回它（保守方向）。
    raw = raw_plan if raw_plan is not None else get_app_settings().quota_plan_free
    plan_limits = _parse_plan(raw)

    limits: dict[str, int | None] = {name: plan_limits.get(name) for name in RESOURCES}
    for name, value in (overrides or {}).items():
        if name not in RESOURCES:
            continue
        if value is None:
            limits[name] = None  # 明確的「這個租戶不限制」
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            limits[name] = value
        else:
            # 壞覆寫保留 plan 值：tenant.settings 是使用者寫得到的 JSON，
            # 一個壞值不該讓那個租戶被鎖死（讀取容忍，寫入驗證屬 2C）。
            logger.warning("quota_override_rejected", resource=name, value=repr(value))
    return limits


def _period_label(resource: str, now: datetime) -> str:
    if resource in _PERIOD_MONTH:
        return f"{now.year:04d}-{now.month:02d}"
    return now.date().isoformat()


def _period_end(resource: str, now: datetime) -> datetime:
    if resource in _PERIOD_MONTH:
        year, month = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
        return datetime(year, month, 1, tzinfo=UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


class QuotaService:
    def __init__(
        self,
        *,
        tenants: TenantRepository | None = None,
        documents: DocumentRepository | None = None,
        notifications: NotificationService | None = None,
    ) -> None:
        self._tenants = tenants or TenantRepository()
        self._documents = documents or DocumentRepository()
        self._notifications = notifications or NotificationService()

    # ── 對外介面（04 §8.1）─────────────────────────────────

    def check_and_reserve(
        self, tenant_id: uuid.UUID, resource: str, amount: int = 1
    ) -> QuotaReservation | None:
        """額度夠就預留並回傳 reservation；不限制回 None；不夠 raise 429。

        存量資源（DB 聚合）只檢查不預留——文件列本身就是紀錄，回 None。
        """
        limit = self.limits(tenant_id).get(resource)
        if limit is None:
            return None

        if resource in _DB_BACKED:
            used = self._db_used(tenant_id, resource)
            self._maybe_notify(
                tenant_id, resource, used=used + amount, limit=limit, now=datetime.now(UTC)
            )
            if used + amount > limit:
                raise self._exceeded(resource, limit=limit, used=used)
            return None

        now = datetime.now(UTC)
        key, ttl = self._key_and_ttl(tenant_id, resource, now)
        client = get_redis()
        # redis-py 的同步 client 標成 Awaitable|Any 聯集，以 cast 收斂（同 auth.py）。
        used_after = cast("int", client.incrby(key, amount))
        client.expire(key, ttl)
        self._maybe_notify(tenant_id, resource, used=used_after, limit=limit, now=now)
        if used_after > limit:
            # INCR-先-檢查是原子的擋線；被擋的那一次立刻回滾，不留下痕跡。
            client.decrby(key, amount)
            raise self._exceeded(
                resource,
                limit=limit,
                used=used_after - amount,
                resets_at=None if resource in _GAUGES else _period_end(resource, now),
            )
        return QuotaReservation(tenant_id=tenant_id, resource=resource, amount=amount, key=key)

    def commit(self, reservation: QuotaReservation, *, actual: int | None = None) -> None:
        """把預留校正為實際值（reserve/commit 的第二段）。actual 為 None＝照預留量。"""
        if actual is None or actual == reservation.amount:
            return
        client = get_redis()
        adjusted = cast("int", client.incrby(reservation.key, actual - reservation.amount))
        if adjusted < 0:
            # 期別在生成途中翻頁（舊 key 過期、調整落在新 key 上）才會出現；
            # 負值沒有語意，夾回 0。
            client.incrby(reservation.key, -adjusted)

    def release(self, reservation: QuotaReservation) -> None:
        """整筆歸還（生成失敗、串流結束）。重複 release 不會把額度變成負的送人。"""
        client = get_redis()
        remaining = cast("int", client.decrby(reservation.key, reservation.amount))
        if remaining < 0:
            client.incrby(reservation.key, -remaining)

    def correct(self, tenant_id: uuid.UUID, resource: str, value: int) -> None:
        """對帳用（2A-2b）：把計數器直接擺成事實值。**方向永遠是 DB 蓋 Redis**，
        反向會把漂移永久化。只對 Redis 計量的資源有意義（存量本來就以 DB 為準）。"""
        now = datetime.now(UTC)
        key, ttl = self._key_and_ttl(tenant_id, resource, now)
        get_redis().set(key, max(value, 0), ex=ttl)

    def status(self, tenant_id: uuid.UUID) -> list[QuotaStatus]:
        """五種資源的即時狀態（`GET /tenants/current/quota` 與 2A-5 通知的資料源）。"""
        limits = self.limits(tenant_id)
        now = datetime.now(UTC)
        client = get_redis()
        statuses: list[QuotaStatus] = []
        for resource in RESOURCES:
            if resource in _DB_BACKED:
                used = self._db_used(tenant_id, resource)
            else:
                key, _ = self._key_and_ttl(tenant_id, resource, now)
                used = int(cast("Any", client.get(key)) or 0)
            limit = limits[resource]
            statuses.append(
                QuotaStatus(
                    resource=resource,
                    limit=limit,
                    used=used,
                    remaining=None if limit is None else max(limit - used, 0),
                    resets_at=(
                        _period_end(resource, now)
                        if resource in _PERIOD_MONTH | _PERIOD_DAY
                        else None
                    ),
                )
            )
        return statuses

    # ── 內部 ────────────────────────────────────────────

    def limits(self, tenant_id: uuid.UUID) -> dict[str, int | None]:
        """這個租戶生效中的限額（plan 預設 → 租戶覆寫）。對帳快照也讀它——
        歷史報表要知道「當時的上限」。"""
        with tenant_context(tenant_id), unit_of_work():
            tenant = self._tenants.current()
            if tenant is None:
                # token 的租戶不存在——沒有東西可以授額度，全部當 0 太苛（會把
                # 資料異常變成全面停機），照「最保守的已知方案」處理。
                return resolve_limits("free")
            overrides = (tenant.settings or {}).get("quota")
            return resolve_limits(
                str(tenant.plan), overrides if isinstance(overrides, dict) else None
            )

    def _db_used(self, tenant_id: uuid.UUID, resource: str) -> int:
        with tenant_context(tenant_id), unit_of_work():
            if resource == "documents":
                return self._documents.active_count()
            return self._documents.active_size_bytes()

    def _maybe_notify(
        self, tenant_id: uuid.UUID, resource: str, *, used: int, limit: int, now: datetime
    ) -> None:
        """跨過門檻就通知 owner／admin（04 §8.5 的 80%／100%，2A-5）。

        **在熱路徑上，因此這裡只做兩件便宜的事**：一次整數比較，以及（真的跨線時）
        一次 DB 查詢 + 寫入。去重靠 `notifications.dedupe_key` 的唯一約束——80%
        之後每一次 reserve 都仍在 80% 以上，不去重的話一天幾百則。

        被擋下的那一次（`used > limit`）也會走到這裡，那是刻意的：使用者最需要
        一則說明的時刻就是被擋住的時候，而 429 只有發出那個請求的人看得到。

        **瞬時值（`_GAUGES`）不通知。** `streams` 記的是「現在同時開著幾條串流」，
        上限 2 的租戶開到第 2 條完全合法，而那一刻就會寄出「同時串流數已用盡」的
        站內信加 email（去重鍵按日，所以每天一封）。訊息本身也是誤導——「超過的請求
        會被擋下」對一秒後就會結束的東西不成立。額度告警要說的是「這一期的量快用完
        了，去加量或省著用」，而瞬時值沒有「這一期」，也沒有什麼可以事先處理。
        """
        if resource in _GAUGES:
            return

        thresholds = quota_thresholds()
        if limit <= 0 or not thresholds or used * 100 < thresholds[0] * limit:
            return

        # **Redis 先擋，再碰 DB。** 80% 之後每一次 reserve 都仍在 80% 以上，而
        # 這裡在使用者的請求路徑上——沒有這道擋板的話，額度快滿的租戶每送一則
        # 訊息就多兩三個 SELECT（收件人 × 門檻）。`SET NX` 是原子的，第一個跨線
        # 的請求負責通知，其餘直接走人。
        #
        # 這不是唯一的保證：真正擋住重複的是 `notifications.dedupe_key` 的唯一
        # 約束（Redis 掉了就重新發一次，而那是可接受的失敗方向）。
        period = _period_label(resource, now)
        crossed = max(t for t in thresholds if used * 100 >= t * limit)
        guard = tenant_key(tenant_id, "quota", "notified", resource, period, str(crossed))
        client = get_redis()
        if not client.set(guard, "1", nx=True, ex=_NOTIFY_GUARD_TTL_SECONDS):
            return

        self._notifications.notify_quota_threshold(
            tenant_id,
            resource,
            used=min(used, limit),
            limit=limit,
            period_start=period,
        )

    def _key_and_ttl(self, tenant_id: uuid.UUID, resource: str, now: datetime) -> tuple[str, int]:
        if resource in _GAUGES:
            return tenant_key(tenant_id, "quota", resource), _GAUGE_TTL_SECONDS
        label = _period_label(resource, now)
        ttl = int((_period_end(resource, now) + _PERIOD_GRACE - now).total_seconds())
        return tenant_key(tenant_id, "quota", resource, label), ttl

    def _exceeded(
        self,
        resource: str,
        *,
        limit: int,
        used: int,
        resets_at: datetime | None = None,
    ) -> QuotaExceededError:
        details: dict[str, Any] = {"resource": resource, "limit": limit, "used": used}
        if resets_at is not None:
            details["resets_at"] = resets_at
        logger.info("quota_exceeded", **details)
        return QuotaExceededError(f"配額已用盡：{resource}（上限 {limit}）", details=details)
