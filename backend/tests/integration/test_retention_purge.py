"""驗收：軟刪除的**保留窗硬刪**（05 §5.4；二次架構審計 P0-2／F-02＋M1）。

軟刪除從 1B 起就承諾「30 天後由清理 job 硬刪」——三處 docstring 都這麼寫，而那個 job
到 2B 為止不存在。這是整份審計裡唯一「後果正在單調累積」的項目：刪掉的 KB／文件／
對話，它們的 chunk、向量、MinIO 物件與訊息全部留著，只增不減。

**KB 級刪除是量最大、也最容易漏掉的一種**：`KnowledgeBaseService.delete` 刻意不逐列
標記底下的文件（那會讓刪除變成長交易），所以那些文件**沒有自己的 `deleted_at`**——
只認 `document.deleted_at` 的清理器會把它們全部漏掉，而 job 照樣全綠。

放 integration 而不是 unit：要驗的是 FK（全是 PROTECT）之下的刪除順序、RLS 之下的
跨租戶邊界、以及物件儲存真的少了那個 key。用假物件驗這一層等於什麼都沒驗。

四件事錯了都不會有錯誤訊息：

1. **保留窗算錯邊**——把還在窗內的資料刪掉。這是全系統第二個不可逆的動作（第一個是
   分區 DROP），沒有任何救回的辦法。
2. **漏刪 chunk／etl_job**——PROTECT 會把文件的刪除擋下，於是 job 每天跑、每天在同一
   批文件上失敗，表繼續長。
3. **漏掉 KB 級刪除的文件**（見上）。
4. **跨租戶**——維運迴圈少了 tenant context 一列都刪不到（RLS fail closed），症狀是
   job 全綠、表照樣長。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.conversation.models import Conversation, MemorySnapshot, Message
from apps.knowledge.models import Chunk, Document, Embedding, EtlJob, KnowledgeBase
from config.settings.app_settings import get_app_settings
from core.object_storage import list_keys, put_object
from services.conversation.purge import DeletedConversationPurgeService
from services.knowledge.purge import DeletedKnowledgePurgeService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.conversation import make_conversation, make_memory_snapshot, make_message
from tests.factories.identity import make_tenant, make_user, tenant_scope
from tests.factories.knowledge import (
    make_chunk,
    make_document,
    make_embedding,
    make_etl_job,
    make_knowledge_base,
)

pytestmark = pytest.mark.django_db(transaction=True)

# 保留窗預設 30 天：31 天前刪的該清，1 天前刪的不該碰。
_LONG_AGO = timedelta(days=31)
_RECENTLY = timedelta(days=1)


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """`get_app_settings` 是 `lru_cache` 的：改過環境變數的測試若不清快取，**後面**
    的測試會拿到它留下的值——而那時失敗的是別人，訊息也指向別處。"""
    get_app_settings.cache_clear()
    yield
    get_app_settings.cache_clear()


@pytest.fixture
def tenants() -> None:
    for tenant_id, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=slug)


def _mark_deleted(
    model: type[Document] | type[KnowledgeBase] | type[Conversation],
    entity_id: uuid.UUID,
    ago: timedelta,
    *,
    tenant_id: uuid.UUID = TENANT_A,
) -> None:
    """直接寫 `deleted_at`——`soft_delete()` 只寫得出「現在」，而這裡要的是過去。

    **一定要在 `tenant_scope` 內**：RLS 之下沒有租戶 context 的 UPDATE 影響 0 列而
    不報錯，於是「刪很久了」根本沒寫進去，整組測試會安靜地變成假綠。
    """
    with tenant_scope(tenant_id):
        updated = model.objects.filter(id=entity_id).update(deleted_at=timezone.now() - ago)
    assert updated == 1, "前提沒設成功：deleted_at 沒有寫進去"


def _document_with_everything(tenant_id: uuid.UUID, *, kb: KnowledgeBase | None = None) -> Document:
    """一份文件 ＋ 兩個 chunk（各一份向量）＋ 一筆 etl_job ＋ 一個真的物件。

    物件用 factory 給的 `storage_key`：它與 `build_document_key` 逐字相同
    （`test_object_storage.py::TestFactoryKeysMatchProduction` 釘住），而 `delete_object`
    每次都比對 `tenant-{tenant_id}/` 前綴——形狀不同會被擋下，那個擋是對的。
    """
    with tenant_scope(tenant_id):
        kb = kb or make_knowledge_base(tenant_id=tenant_id)
        document = make_document(kb=kb, status="ready")
        key = str(document.storage_key)
        for seq in range(2):
            make_embedding(chunk=make_chunk(document=document, seq=seq))
        make_etl_job(document=document, stage="parse", status="succeeded")
        put_object(key, b"%PDF-1.4 test", content_type="application/pdf")
        return document


def _knowledge_counts(tenant_id: uuid.UUID) -> tuple[int, int, int, int, int]:
    """(KB, 文件, chunk, 向量, etl_job)——**含已軟刪除的列**，這裡驗的是硬刪。"""
    with tenant_scope(tenant_id):
        return (
            KnowledgeBase.objects.count(),
            Document.objects.count(),
            Chunk.objects.count(),
            Embedding.objects.count(),
            EtlJob.objects.count(),
        )


def _object_exists(key: str) -> bool:
    with tenant_scope(TENANT_A):
        return key in list_keys(key)


class TestDocumentRetention:
    def test_a_document_deleted_long_ago_is_fully_purged(self, tenants: None) -> None:
        """文件、chunk、向量、etl_job、物件——五樣一起消失，少一樣都是漏刪。"""
        document = _document_with_everything(TENANT_A)
        key = str(document.storage_key)
        _mark_deleted(Document, uuid.UUID(str(document.id)), _LONG_AGO)

        counts = DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A)

        assert counts.documents == 1
        assert counts.chunks == 2
        assert counts.objects == 1
        assert _knowledge_counts(TENANT_A)[1:] == (0, 0, 0, 0)
        assert not _object_exists(key), "MinIO 的位元組也要走（05 §5.4 的級聯含物件）"

    def test_a_document_still_inside_the_window_is_untouched(self, tenants: None) -> None:
        """保留窗的意義就是「使用者可能後悔」。刪早了沒有任何救回的辦法。"""
        document = _document_with_everything(TENANT_A)
        _mark_deleted(Document, uuid.UUID(str(document.id)), _RECENTLY)

        counts = DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A)

        assert counts.documents == 0
        assert _knowledge_counts(TENANT_A) == (1, 1, 2, 2, 1)

    def test_a_live_document_is_untouched(self, tenants: None) -> None:
        _document_with_everything(TENANT_A)

        DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A)

        assert _knowledge_counts(TENANT_A) == (1, 1, 2, 2, 1)

    def test_it_is_idempotent(self, tenants: None) -> None:
        document = _document_with_everything(TENANT_A)
        _mark_deleted(Document, uuid.UUID(str(document.id)), _LONG_AGO)
        service = DeletedKnowledgePurgeService()
        service.purge_for_tenant(TENANT_A)

        assert service.purge_for_tenant(TENANT_A).documents == 0

    def test_other_tenants_are_untouched(self, tenants: None) -> None:
        """維運迴圈逐租戶進 context；少了它 RLS 會 fail closed（一列都刪不到），
        而多了別人的資料則是最壞的一種 bug。"""
        document = _document_with_everything(TENANT_A)
        _mark_deleted(Document, uuid.UUID(str(document.id)), _LONG_AGO)
        other = _document_with_everything(TENANT_B)
        _mark_deleted(Document, uuid.UUID(str(other.id)), _LONG_AGO, tenant_id=TENANT_B)

        DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A)

        assert _knowledge_counts(TENANT_B) == (1, 1, 2, 2, 1)


class TestKnowledgeBaseRetention:
    def test_deleting_a_kb_purges_the_documents_it_never_marked(self, tenants: None) -> None:
        """**F-02 的正題**：KB 級刪除在文件表上不留任何痕跡。

        `KnowledgeBaseService.delete` 只軟刪 KB 那一列（逐一標記數萬份文件會讓刪除
        變成長交易）。只認 `document.deleted_at` 的清理器會把它們全部漏掉，而那正是
        量最大的一種——一個 KB 底下可能有數千份文件、數十萬個向量。
        """
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A)
        _document_with_everything(TENANT_A, kb=kb)
        _mark_deleted(KnowledgeBase, uuid.UUID(str(kb.id)), _LONG_AGO)

        counts = DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A)

        assert counts.documents == 1, "文件自己沒有 deleted_at，只認那一欄就會全部漏掉"
        assert counts.knowledge_bases == 1
        assert _knowledge_counts(TENANT_A) == (0, 0, 0, 0, 0)

    def test_a_kb_still_holding_documents_survives_the_round(
        self, tenants: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`Document.kb` 是 PROTECT：文件還沒清完就不能刪 KB。

        批次上限之下這是**正常情況**（一個積欠半年的租戶不可能一輪清完），所以它必須
        是「這一輪跳過 KB」而不是「整輪炸掉」——後者會讓那個租戶的清理永遠停在同一個
        地方，而 log 裡只有一個 FK 錯誤。下一輪把剩下的文件清完，KB 就跟著走。
        """
        monkeypatch.setenv("RETENTION_PURGE_BATCH_SIZE", "1")
        get_app_settings.cache_clear()
        with tenant_scope(TENANT_A):
            kb = make_knowledge_base(tenant_id=TENANT_A)
        _document_with_everything(TENANT_A, kb=kb)
        _document_with_everything(TENANT_A, kb=kb)
        _mark_deleted(KnowledgeBase, uuid.UUID(str(kb.id)), _LONG_AGO)

        service = DeletedKnowledgePurgeService()
        first = service.purge_for_tenant(TENANT_A)

        assert (first.documents, first.knowledge_bases) == (1, 0), "一輪一批，KB 還不能刪"
        assert _knowledge_counts(TENANT_A)[:2] == (1, 1)

        second = service.purge_for_tenant(TENANT_A)

        assert (second.documents, second.knowledge_bases) == (1, 1), "下一輪收尾"
        assert _knowledge_counts(TENANT_A) == (0, 0, 0, 0, 0)

    def test_a_live_kb_is_untouched(self, tenants: None) -> None:
        with tenant_scope(TENANT_A):
            make_knowledge_base(tenant_id=TENANT_A)

        assert DeletedKnowledgePurgeService().purge_for_tenant(TENANT_A).knowledge_bases == 0
        assert _knowledge_counts(TENANT_A)[0] == 1


class TestConversationRetention:
    """M1：對話的刪除路徑同樣承諾了一個不存在的清理者（`conversations.py:185`）。

    量與成本都遠小於 KB／文件，但**訊息是使用者內容**——留著的是保留合規問題。
    """

    def _conversation_with_messages(self, tenant_id: uuid.UUID) -> Conversation:
        with tenant_scope(tenant_id):
            user = make_user(tenant_id=tenant_id, email=f"o@{tenant_id}.example")
            conversation = make_conversation(tenant_id=tenant_id, user=user)
            for _ in range(3):
                make_message(conversation=conversation)
            make_memory_snapshot(conversation=conversation)
            return conversation

    def _counts(self, tenant_id: uuid.UUID) -> tuple[int, int, int]:
        with tenant_scope(tenant_id):
            return (
                Conversation.objects.count(),
                Message.objects.count(),
                MemorySnapshot.objects.count(),
            )

    def test_a_conversation_deleted_long_ago_takes_its_messages_with_it(
        self, tenants: None
    ) -> None:
        conversation = self._conversation_with_messages(TENANT_A)
        _mark_deleted(Conversation, uuid.UUID(str(conversation.id)), _LONG_AGO)

        counts = DeletedConversationPurgeService().purge_for_tenant(TENANT_A)

        assert (counts.conversations, counts.messages) == (1, 3)
        assert self._counts(TENANT_A) == (0, 0, 0), "摘要也要走（PROTECT，漏了對話刪不掉）"

    def test_a_conversation_still_inside_the_window_is_untouched(self, tenants: None) -> None:
        conversation = self._conversation_with_messages(TENANT_A)
        _mark_deleted(Conversation, uuid.UUID(str(conversation.id)), _RECENTLY)

        assert DeletedConversationPurgeService().purge_for_tenant(TENANT_A).conversations == 0
        assert self._counts(TENANT_A) == (1, 3, 1)

    def test_a_live_conversation_is_untouched(self, tenants: None) -> None:
        self._conversation_with_messages(TENANT_A)

        DeletedConversationPurgeService().purge_for_tenant(TENANT_A)

        assert self._counts(TENANT_A) == (1, 3, 1)
