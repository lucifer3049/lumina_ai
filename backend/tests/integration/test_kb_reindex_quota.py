"""驗收：整庫重建的額度預檢（2B-6 缺口④，人類裁決 2026-08-28：**超額就擋下**）。

重建是這套系統裡**單次花費最大的動作**：一個知識庫可能有數萬個 chunk，而每一個都是
一次真的 embedding API 呼叫。它由管理員按一下觸發，而按下去的那個人看不到帳單。

`documents` 那條路（上傳）從 2A-2a 起就先檢查額度再落地；重建至今沒有——它把用量記
進 `usage_logs`（看得到花了多少），但不擋。

三個決定，每一個都有一種「看起來對但錯」的做法：

1. **估算用 `chunk.token_count` 的總和**，不是 chunk 數乘一個常數。那一欄是切塊時算
   的實際 token 數（1B-5 的 chunker 注入），而 chunk 的長度差距很大——用平均值估，
   一個放滿長表格的知識庫會被低估到擋不住。
2. **只檢查、不預留**。`tokens_month` 的事實來源是 `usage_logs` 裡 `category="llm"`
   的列（`UsageLogRepository.llm_token_total`），而 embedding 記的是
   `category="embedding"`——預留下去的數字**隔天會被日結對帳抹掉**（2A-2b 的鐵律：
   DB 蓋 Redis）。那會留下一個沒有人對得起來的計數器，而它的方向是「先擋了使用者
   一整天的 chat，隔天自己消失」。
3. **被擋的請求什麼都不留**（同上傳路徑）：不建 job。留一筆 failed 的話，那個 KB 的
   重建歷史會被一堆「從來沒開始過」的紀錄塞滿，而使用者以為重建失敗了。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from apps.knowledge.models import KbReindexJob
from services.platform.quota import QuotaExceededError, QuotaService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)

# 這個租戶的月額度（由 `tenant.settings` 覆寫，不動 plan 預設）。
_LIMIT = 1000


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    from core.redis import get_redis, tenant_key

    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def tenant() -> None:
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a", settings={"quota": {"tokens_month": _LIMIT}})


def _kb_with_tokens(*, tokens: int, chunks: int = 2, superseded_tokens: int = 0) -> uuid.UUID:
    """一個 KB，其現行 chunk 的 `token_count` 總和 = `tokens`。"""
    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A, embedding_model="m", embedding_version=1)
        document = make_document(kb=kb, status="ready")
        per_chunk = tokens // chunks
        for seq in range(chunks):
            make_chunk(document=document, seq=seq, token_count=per_chunk)
        if superseded_tokens:
            make_chunk(
                document=document,
                seq=90,
                doc_version=1,
                superseded=True,
                token_count=superseded_tokens,
            )
        return uuid.UUID(str(kb.id))


def _service() -> Any:
    from ai.gateway import AIGateway
    from services.knowledge.reindex import KbReindexService

    return KbReindexService(gateway=AIGateway())


def _use(tokens: int) -> None:
    """把當月的 token 計數器推到 `tokens`（模擬這個月已經聊掉的量）。"""
    QuotaService().correct(TENANT_A, "tokens_month", tokens)


def _jobs() -> int:
    with tenant_scope(TENANT_A):
        return KbReindexJob.objects.count()


class TestBlocked:
    def test_a_reindex_that_does_not_fit_is_rejected(self, tenant: None) -> None:
        """剩 100，這次要 800——擋下（429）。"""
        kb_id = _kb_with_tokens(tokens=800)
        _use(_LIMIT - 100)

        with pytest.raises(QuotaExceededError) as exceeded:
            _service().start(TENANT_A, kb_id, target_model="new-model")

        details = exceeded.value.details
        assert details["resource"] == "tokens_month"
        # client 要能說出「還差多少」，否則畫面只能顯示一句「額度不足」。
        assert details["limit"] == _LIMIT
        assert details["needed"] == 800

    def test_a_blocked_request_leaves_nothing_behind(self, tenant: None) -> None:
        """不建 job：留一筆 failed 的話，重建歷史會被「從來沒開始過」的紀錄塞滿。"""
        kb_id = _kb_with_tokens(tokens=800)
        _use(_LIMIT - 100)

        with pytest.raises(QuotaExceededError):
            _service().start(TENANT_A, kb_id, target_model="new-model")

        assert _jobs() == 0

    def test_an_exhausted_quota_blocks_even_a_tiny_kb(self, tenant: None) -> None:
        """額度已經滿了，再小的重建也不放行——那是「超額就擋下」的字面意思。"""
        kb_id = _kb_with_tokens(tokens=2, chunks=2)
        _use(_LIMIT)

        with pytest.raises(QuotaExceededError):
            _service().start(TENANT_A, kb_id, target_model="new-model")


class TestAllowed:
    def test_a_reindex_that_fits_goes_through(self, tenant: None) -> None:
        kb_id = _kb_with_tokens(tokens=100)
        _use(_LIMIT - 500)

        job = _service().start(TENANT_A, kb_id, target_model="new-model")

        assert job.status == "pending"
        assert _jobs() == 1

    def test_an_unlimited_tenant_is_never_blocked(self) -> None:
        """`None` = 這個租戶不限制（`resolve_limits`）。拿 None 去比大小會 TypeError，
        而那個例外會在**成功路徑**上炸掉一個本來就該放行的請求。"""
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a", settings={"quota": {"tokens_month": None}})
        kb_id = _kb_with_tokens(tokens=10**9)

        assert _service().start(TENANT_A, kb_id, target_model="new-model").status == "pending"

    def test_an_empty_kb_costs_nothing(self, tenant: None) -> None:
        """沒有 chunk 就沒有要算的向量——額度用盡也該讓它跑完（它會秒收）。"""
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A, embedding_model="m")
            kb_id = uuid.UUID(str(kb.id))
        _use(_LIMIT)

        assert _service().start(TENANT_A, kb_id, target_model="new-model").status == "pending"


class TestEstimate:
    def test_superseded_chunks_are_not_part_of_the_bill(self, tenant: None) -> None:
        """舊版 chunk 不會被重算（`_embed_missing` 走 `for_retrieval`），估算也不該算它。

        算進去的話，一個重跑過很多次的知識庫會被高估到永遠開不了重建——而那些 chunk
        幾天後就被清理 job 硬刪了。
        """
        kb_id = _kb_with_tokens(tokens=100, superseded_tokens=10_000)
        _use(_LIMIT - 200)

        assert _service().start(TENANT_A, kb_id, target_model="new-model").status == "pending"

    def test_the_check_does_not_consume_the_budget(self, tenant: None) -> None:
        """**只檢查、不預留**（本檔第 2 條）。

        預留的話，那個數字隔天會被日結對帳抹掉（`tokens_month` 的事實來源只認
        `category="llm"`），而在那之前它擋著這個租戶的每一次對話。
        """
        kb_id = _kb_with_tokens(tokens=100)
        _use(400)

        _service().start(TENANT_A, kb_id, target_model="new-model")

        used = next(s.used for s in QuotaService().status(TENANT_A) if s.resource == "tokens_month")
        assert used == 400, "預檢不得動到計數器"
