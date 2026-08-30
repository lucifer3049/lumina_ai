"""驗收：`TurnBudget.reserve` 的自我清理涵蓋**所有**失敗，不只被擋（2026-08-30 深度審查）。

`reserve` 依序過三關（messages_day → tokens_month → streams），原本只在
`QuotaExceededError` 時把已預留的還回去。但第二、三關失敗的原因不只「被擋」——
Redis 斷線、DB 逾時都會從 `check_and_reserve` 丟出基礎設施例外，而那條路徑上
第一關已經扣掉了。

不清理的話沒有任何症狀：使用者看到 500，重試就過了；洩漏的預留要等期別翻頁
才自己消失，而 tokens_month 的窗是**一個月**——Redis 抖一個下午，這個租戶的
月額度就短少一截，且 usage_logs 裡沒有任何一筆帳對得上。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from services.conversation.budget import TurnBudget
from services.platform.quota import QuotaExceededError, QuotaReservation

TENANT = uuid.UUID("11111111-1111-5111-8111-111111111111")


class _FlakyQuota:
    """`QuotaService` 替身：指定的資源那一關丟出指定的例外，其餘照常預留。"""

    def __init__(self, *, fail_on: str, exc: Exception) -> None:
        self._fail_on = fail_on
        self._exc = exc
        self.released: list[QuotaReservation] = []

    def check_and_reserve(
        self, tenant_id: uuid.UUID, resource: str, amount: int
    ) -> QuotaReservation | None:
        if resource == self._fail_on:
            raise self._exc
        return QuotaReservation(
            tenant_id=tenant_id,
            resource=resource,
            amount=amount,
            key=f"t:{tenant_id}:quota:{resource}",
            ttl_seconds=60,
        )

    def release(self, reservation: QuotaReservation) -> None:
        self.released.append(reservation)


def _budget(quota: _FlakyQuota) -> TurnBudget:
    return TurnBudget(quota=quota)  # type: ignore[arg-type]


class TestReserveCleansUpOnAnyFailure:
    def test_an_infrastructure_error_mid_reserve_releases_what_was_acquired(self) -> None:
        """第二關 Redis 斷線：例外照樣往上拋，但第一關的預留必須已經還回去。"""
        quota = _FlakyQuota(fail_on="tokens_month", exc=ConnectionError("redis 斷線"))

        with pytest.raises(ConnectionError):
            _budget(quota).reserve(TENANT, question="測試問題")

        assert [r.resource for r in quota.released] == ["messages_day"]

    def test_a_failure_at_the_last_gate_releases_both_earlier_reservations(self) -> None:
        quota = _FlakyQuota(fail_on="streams", exc=TimeoutError("db 逾時"))

        with pytest.raises(TimeoutError):
            _budget(quota).reserve(TENANT, question="測試問題")

        assert [r.resource for r in quota.released] == ["messages_day", "tokens_month"]

    def test_quota_exceeded_still_cleans_up_and_still_raises_its_own_type(self) -> None:
        """原本的行為不變：被擋走的仍是 `QuotaExceededError`，呼叫端的 429 對映不受影響。"""
        quota = _FlakyQuota(fail_on="streams", exc=QuotaExceededError("並發已滿"))

        with pytest.raises(QuotaExceededError):
            _budget(quota).reserve(TENANT, question="測試問題")

        assert [r.resource for r in quota.released] == ["messages_day", "tokens_month"]


class TestSettleWithEstimatedUsage:
    """stop 路徑會帶**估算的** usage 來結算（見 `ChatService._estimated_usage`）——
    settle 對它的處置必須與真實 usage 相同：commit 校正，而不是整筆退回。"""

    async def test_estimated_usage_commits_instead_of_releasing(self) -> None:
        committed: list[tuple[QuotaReservation, int | None]] = []
        released: list[QuotaReservation] = []

        class _Quota:
            def commit(self, reservation: QuotaReservation, *, actual: int | None = None) -> None:
                committed.append((reservation, actual))

            def release(self, reservation: QuotaReservation) -> None:
                released.append(reservation)

        reservation = QuotaReservation(
            tenant_id=TENANT, resource="tokens_month", amount=2000, key="k", ttl_seconds=60
        )
        usage: dict[str, Any] = {
            "prompt_tokens": 40,
            "completion_tokens": 12,
            "cost": None,
            "estimated": True,
        }

        await TurnBudget(quota=_Quota()).settle_tokens(reservation, usage)  # type: ignore[arg-type]

        assert committed == [(reservation, 52)]
        assert released == []
