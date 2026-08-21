"""驗收：embedding 的 usage 落地（04 §8.2、06 §4，13 §4 工作包 2A-1）。

1C-3 之後 embedding 的 tokens 只活在 task 的回傳值與 log 裡——Quota（2A-2）與
Analytics（2A-3）都看不到它。這裡驗 `EmbeddingService` 跑完之後 usage_logs 有據可查。

與 chat 那一側（tests/api/test_usage_recording.py）的差異：embedding 是**系統行為**
（user_id 空、沒有 conversation），且**冪等重跑不能重複計費入帳**——tokens 記的是
「這一次真的送去算的」，已有向量的 chunk 不再送、也就不再記。

方法論借 test_embedding_pipeline.py 的 `_CountingProvider`：provider 回報的 tokens
是斷言的基準，service 記多少必須與 provider 收多少一致——兩者對不上就是有一段
消費沒有進帳，而那不會有任何症狀。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderEmbedding
from apps.platform.models import UsageLog
from config.settings.app_settings import get_app_settings
from services.knowledge.embedding import EmbeddingService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.identity import make_tenant, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base

pytestmark = pytest.mark.django_db(transaction=True)


class _CountingProvider:
    name = "counting"

    def __init__(self) -> None:
        self.reported_tokens = 0

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        dimensions = get_app_settings().ai_embedding_dimensions
        tokens = sum(len(text) for text in texts)
        self.reported_tokens += tokens
        return ProviderEmbedding(
            vectors=[[0.1] * dimensions for _ in texts],
            model=model,
            prompt_tokens=tokens,
        )


def _service(provider: _CountingProvider) -> EmbeddingService:
    return EmbeddingService(
        gateway=AIGateway(embedding_provider=provider, retry_backoff_seconds=())
    )


@pytest.fixture
def tenants() -> None:
    for tenant_id, name in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{name}")


def _chunked_document(tenant_id: uuid.UUID, *, chunk_count: int, **kb_fields: Any) -> uuid.UUID:
    with tenant_scope(tenant_id):
        kb = make_knowledge_base(tenant_id=tenant_id, **kb_fields)
        document = make_document(kb=kb, status="chunked")
        for seq in range(chunk_count):
            make_chunk(document=document, seq=seq, content=f"第 {seq} 段內容")
        return uuid.UUID(str(document.id))


def _usage_rows(tenant_id: uuid.UUID) -> list[UsageLog]:
    with tenant_scope(tenant_id):
        return list(UsageLog.objects.filter(category="embedding"))


class TestEmbeddingUsageRecording:
    def test_a_run_lands_one_row_with_the_reported_tokens(self, tenants: None) -> None:
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=5)

        _service(provider).embed_document(TENANT_A, document_id)

        rows = _usage_rows(TENANT_A)
        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_tokens == provider.reported_tokens
        assert row.completion_tokens == 0
        assert row.user_id is None, "embedding 是系統行為，不掛在任何使用者名下"
        assert str(document_id) in row.request_id, (
            "request_id 必須對映得回文件——對帳查『這份文件花了多少』要靠它"
        )

    def test_the_model_is_the_one_the_provider_reported(self, tenants: None) -> None:
        """記 provider 回報的 model 而不是我們送的（06 §4 的別名解析，
        同 embeddings 表唯一鍵的理由）：計價按實際被用到的那一個。"""
        document_id = _chunked_document(TENANT_A, chunk_count=2, embedding_model="alias-model")

        _service(_CountingProvider()).embed_document(TENANT_A, document_id)

        rows = _usage_rows(TENANT_A)
        assert len(rows) == 1
        assert rows[0].model == "alias-model"

    def test_a_rerun_with_nothing_to_embed_records_nothing(self, tenants: None) -> None:
        """冪等重跑（task 重試、手動重推）沒有新消費就沒有新列。
        記 0 的列會灌水「呼叫次數」這個統計，而重試不是使用者的行為。"""
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_A, chunk_count=3)
        service = _service(provider)
        service.embed_document(TENANT_A, document_id)

        service.embed_document(TENANT_A, document_id)

        assert len(_usage_rows(TENANT_A)) == 1

    def test_rows_land_in_the_right_tenant(self, tenants: None) -> None:
        """usage 的歸屬錯租戶＝把 A 的帳算到 B 頭上。RLS 擋讀不擋「寫進自己名下
        但算錯人」——歸屬正確要在寫入端驗。"""
        provider = _CountingProvider()
        document_id = _chunked_document(TENANT_B, chunk_count=2)

        _service(provider).embed_document(TENANT_B, document_id)

        assert _usage_rows(TENANT_A) == []
        assert len(_usage_rows(TENANT_B)) == 1
