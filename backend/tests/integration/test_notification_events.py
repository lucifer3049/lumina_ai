"""驗收：什麼事情會產生通知（04 §8.5、08 §6、13 §4 工作包 2A-5）。

「通知」不是一張表就算做完了——真正的價值在**接線**：ETL 進了 DLQ、文件終於
ready、額度快用完，這三件事從 1B／1C／2A-2a 起就寫在 DB 裡，而使用者到今天為止
一次都沒有被告知過。13 §3 的三張結案表各記了一次「通知排 2A」（1B 缺口①、1C
缺口③、Phase 2 缺口⑤），這一包是那三筆的落點。

**收件人是「上傳者」**（開工前人類裁示）：`documents` 原本沒有這個欄位（05 §3.2
的欄位表也沒有），本包新增 `uploaded_by`，走三步走的第一步（可為 NULL 的新欄位）
——**舊文件的 NULL 退回寄給 owner／admin**，因為「沒有人收到」比「收件人不精確」
糟得多：DLQ 的存在意義就是有人去修。

四件錯了都不會有例外：

1. **失敗沒有接線**。文件停在 failed、通知表空的，而使用者的畫面與「還在處理中」
   長得一模一樣。這正是 2A 之前的現況——沒有紅燈，只有沉默。
2. **收合把不同的事合成一件**。同一個 KB、同一個時間窗才收合；跨 KB 收合會讓
   「法規手冊完成了」與「新人訓練完成了」變成一句「2 份文件已完成」，而使用者
   點進去的是錯的地方。
3. **quota 告警每次呼叫都發**。80% 之後每一次 reserve 都跨線，一天幾百則——
   而它們每一則看起來都是對的。
4. **內部錯誤訊息被端到使用者面前**。`error_payload` 對基礎設施錯誤只給分類、
   不給原文（1B 已定案），通知若自己去讀 exception 就把那道牆繞過去了。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from services.platform.notifications import (
    TYPE_DOCUMENT_FAILED,
    TYPE_DOCUMENT_READY,
    TYPE_QUOTA_THRESHOLD,
)

from ai.gateway import AIGateway
from ai.gateway.providers import ProviderEmbedding
from apps.identity.models import Role
from repositories.platform import NotificationRepository
from services.knowledge.documents import DocumentService
from services.knowledge.embedding import EmbeddingService
from services.knowledge.ingestion import IngestionService
from services.platform.quota import QuotaExceededError, QuotaService
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True)

_MARKDOWN = """# 第一章 總則

本章說明適用範圍，內容足夠長以便產生至少一個 chunk。
""".encode()


class _Provider:
    """假 embedding provider（CLAUDE.md：LLM 測試一律 mock）。"""

    name = "fake"

    def embed(self, texts: list[str], *, model: str, timeout_seconds: float) -> ProviderEmbedding:
        from config.settings.app_settings import get_app_settings

        dimensions = get_app_settings().ai_embedding_dimensions
        return ProviderEmbedding(
            vectors=[[0.1] * dimensions for _ in texts],
            model=model,
            prompt_tokens=len(texts),
        )


@pytest.fixture
def uploader() -> uuid.UUID:
    """一個上傳者，外加 owner／admin／editor 三個人（收件人判定的載具）。"""
    ensure_identity_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug="tenant-a")
        people: dict[str, uuid.UUID] = {}
        for role_name in ("owner", "admin", "editor"):
            user = make_user(tenant_id=TENANT_A, email=f"{role_name}@example.com")
            make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name=role_name))
            people[role_name] = uuid.UUID(str(user.id))
    return people["editor"]


def _people() -> dict[str, uuid.UUID]:
    """email 的前綴就是角色名（見 fixture）——省掉在測試之間傳一個 dict。"""
    from apps.identity.models import User

    with tenant_scope(TENANT_A):
        return {
            str(user.email).split("@")[0]: uuid.UUID(str(user.id)) for user in User.objects.all()
        }


def _inbox(user_id: uuid.UUID) -> list[Any]:
    from core.tenant import tenant_context
    from core.uow import unit_of_work

    with tenant_context(TENANT_A), unit_of_work():
        rows, _ = NotificationRepository().inbox(user_id=user_id, limit=50)
    return list(rows)


def _upload(
    uploaded_by: uuid.UUID | None,
    *,
    kb_id: uuid.UUID | None = None,
    content: bytes = _MARKDOWN,
    suffix: str = "md",
) -> uuid.UUID:
    if kb_id is None:
        with tenant_scope(TENANT_A):
            kb_id = uuid.UUID(str(make_knowledge_base(tenant_id=TENANT_A).id))
    view = DocumentService().upload(
        TENANT_A,
        kb_id,
        filename=f"{uuid.uuid4().hex}.{suffix}",
        content=content,
        uploaded_by=uploaded_by,
    )
    return uuid.UUID(str(view.id))


class TestDocumentFailed:
    def test_the_uploader_hears_about_a_dead_lettered_document(self, uploader: uuid.UUID) -> None:
        """重試耗盡 = 08 §6 的 DLQ。文件標 failed 之外要有人被告知。"""
        document_id = _upload(uploader)

        IngestionService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("物件儲存連不上"), attempts=3
        )

        rows = _inbox(uploader)
        assert [row.type for row in rows] == [TYPE_DOCUMENT_FAILED]
        assert rows[0].meta["document_id"] == str(document_id)
        assert rows[0].meta["retryable"] is True
        assert rows[0].meta["stage"] == "extract"

    def test_a_poisoned_document_also_reaches_its_uploader(self, uploader: uuid.UUID) -> None:
        """毒檔不重試（08 §6），因此它**只會**經永久失敗這條路——只接重試耗盡
        那條的話，使用者上傳一份壞檔之後永遠等不到任何回音。"""
        document_id = _upload(uploader, content=b"%PDF-1.7\nnot really a pdf", suffix="pdf")

        IngestionService().ingest(TENANT_A, document_id)

        rows = _inbox(uploader)
        assert [row.type for row in rows] == [TYPE_DOCUMENT_FAILED]
        assert rows[0].meta["retryable"] is False

    def test_an_embedding_failure_notifies_too(self, uploader: uuid.UUID) -> None:
        """1C 的 embedding worker 有自己的 DLQ 落點（`EmbeddingService`）——
        兩條失敗路徑要各自接線，接一條的症狀是「有些失敗會通知、有些不會」。"""
        document_id = _upload(uploader)
        IngestionService().ingest(TENANT_A, document_id)

        EmbeddingService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("provider 連不上"), attempts=3
        )

        rows = _inbox(uploader)
        assert [row.type for row in rows] == [TYPE_DOCUMENT_FAILED]
        assert rows[0].meta["stage"] == "embed"

    def test_the_internal_error_message_never_reaches_the_notification(
        self, uploader: uuid.UUID
    ) -> None:
        """基礎設施錯誤只給分類、不給原文（1B 定案，`error_payload`）——
        通知是這道牆最容易被繞過的地方，它離使用者最近。"""
        document_id = _upload(uploader)

        IngestionService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("password=hunter2 host=10.0.0.7"), attempts=3
        )

        row = _inbox(uploader)[0]
        assert "hunter2" not in row.body
        assert "hunter2" not in str(row.meta)

    def test_an_old_document_without_an_uploader_falls_back_to_the_admins(
        self, uploader: uuid.UUID
    ) -> None:
        """`uploaded_by` 是本包新增的欄位，既有文件一律 NULL。沒有人收到通知
        比收件人不精確糟得多——DLQ 的意義就是有人去修。"""
        document_id = _upload(None)

        IngestionService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("物件儲存連不上"), attempts=3
        )

        people = _people()
        assert len(_inbox(people["owner"])) == 1
        assert len(_inbox(people["admin"])) == 1
        assert _inbox(people["editor"]) == []


class TestDocumentReady:
    def _embed(self, document_id: uuid.UUID) -> None:
        EmbeddingService(gateway=AIGateway(embedding_provider=_Provider())).embed_document(
            TENANT_A, document_id
        )

    def test_the_uploader_is_told_when_the_document_becomes_answerable(
        self, uploader: uuid.UUID
    ) -> None:
        """08 §2 的狀態機終點是 `ready`——那是「可以問了」的那一刻，也是唯一
        值得通知的一刻（chunked 對使用者沒有意義）。"""
        document_id = _upload(uploader)
        IngestionService().ingest(TENANT_A, document_id)

        self._embed(document_id)

        rows = _inbox(uploader)
        assert [row.type for row in rows] == [TYPE_DOCUMENT_READY]
        assert rows[0].meta["count"] == 1

    def test_a_batch_upload_collapses_into_a_single_notification(self, uploader: uuid.UUID) -> None:
        """一次上傳 50 份就是 50 則——04 §8.5 的「去重與節流」要在這裡兌現。
        同一個 KB、同一個時間桶內的 ready 合成一列，計數往上加。"""
        with tenant_scope(TENANT_A):
            kb_id = uuid.UUID(str(make_knowledge_base(tenant_id=TENANT_A).id))
        for _ in range(3):
            document_id = _upload(uploader, kb_id=kb_id)
            IngestionService().ingest(TENANT_A, document_id)
            self._embed(document_id)

        rows = _inbox(uploader)
        assert len(rows) == 1
        assert rows[0].meta["count"] == 3

    def test_another_knowledge_base_gets_its_own_notification(self, uploader: uuid.UUID) -> None:
        """收合的邊界是 KB：合過頭的話，「法規」與「新人訓練」會變成一句話，
        而使用者點進去的是錯的地方。"""
        for _ in range(2):
            document_id = _upload(uploader)
            IngestionService().ingest(TENANT_A, document_id)
            self._embed(document_id)

        assert len(_inbox(uploader)) == 2


class TestQuotaThreshold:
    """80%／100% 的告警（04 §8.5）。觸發點在 `check_and_reserve`（開工前人類裁示）
    ——每日對帳才算的話，「快爆了」最多晚一天才知道，而 80% 這個門檻的全部意義
    就是提前。"""

    @pytest.fixture
    def small_quota(self) -> uuid.UUID:
        ensure_identity_seed()
        with tenant_scope(TENANT_A):
            make_tenant(id=TENANT_A, slug="tenant-a", settings={"quota": {"tokens_month": 10}})
            owner = make_user(tenant_id=TENANT_A, email="owner@example.com")
            make_user_role(user=owner, role=Role.objects.get(tenant__isnull=True, name="owner"))
            editor = make_user(tenant_id=TENANT_A, email="editor@example.com")
            make_user_role(user=editor, role=Role.objects.get(tenant__isnull=True, name="editor"))
            return uuid.UUID(str(owner.id))

    def test_crossing_eighty_percent_tells_the_owner(self, small_quota: uuid.UUID) -> None:
        QuotaService().check_and_reserve(TENANT_A, "tokens_month", 8)

        rows = _inbox(small_quota)
        assert [row.type for row in rows] == [TYPE_QUOTA_THRESHOLD]
        assert rows[0].meta["resource"] == "tokens_month"
        assert rows[0].meta["threshold"] == 80

    def test_a_plain_member_is_not_told(self, small_quota: uuid.UUID) -> None:
        """額度是管理面資訊（界線同 `analytics:read`）：editor 收到的話，
        等於把公司的消費輪廓發給每一個人。"""
        QuotaService().check_and_reserve(TENANT_A, "tokens_month", 8)

        people = _people()
        assert _inbox(people["editor"]) == []

    def test_staying_below_the_threshold_says_nothing(self, small_quota: uuid.UUID) -> None:
        QuotaService().check_and_reserve(TENANT_A, "tokens_month", 1)

        assert _inbox(small_quota) == []

    def test_the_same_threshold_only_fires_once_per_period(self, small_quota: uuid.UUID) -> None:
        """80% 之後每一次 reserve 都仍在 80% 以上——不去重的話一天幾百則，
        而每一則看起來都是對的。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 8)
        service.check_and_reserve(TENANT_A, "tokens_month", 1)

        assert len(_inbox(small_quota)) == 1

    def test_reaching_the_limit_fires_the_second_threshold(self, small_quota: uuid.UUID) -> None:
        """80% 與 100% 是兩件事：前者是「該注意了」，後者是「已經被擋住了」。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 8)
        service.check_and_reserve(TENANT_A, "tokens_month", 2)

        assert sorted(row.meta["threshold"] for row in _inbox(small_quota)) == [80, 100]

    def test_being_blocked_also_leaves_a_notification(self, small_quota: uuid.UUID) -> None:
        """被 429 擋下的那一次不會留下計數（reserve 會回滾），但它正是使用者
        最需要一則說明的時刻——擋在 API 層的 429 只有那一個請求看得到。"""
        service = QuotaService()
        service.check_and_reserve(TENANT_A, "tokens_month", 10)
        with pytest.raises(QuotaExceededError):
            service.check_and_reserve(TENANT_A, "tokens_month", 5)

        assert sorted(row.meta["threshold"] for row in _inbox(small_quota)) == [80, 100]


class TestEmailDispatch:
    """email 是**旁路**：它離開請求路徑，也不准把主流程弄壞（同 2A-4 的稽核）。"""

    def test_the_letter_leaves_through_the_queue(
        self, uploader: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """寄信寫在請求路徑上時，SMTP 連不上的那幾秒是使用者在等——而他等的是
        一件與他按的按鈕無關的事。"""
        import services.platform.notifications as module

        sent: list[uuid.UUID] = []
        monkeypatch.setattr(
            module,
            "enqueue_notification_email",
            lambda *, tenant_id, notification_id: sent.append(notification_id),
        )
        document_id = _upload(uploader)

        IngestionService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("物件儲存連不上"), attempts=3
        )

        assert sent == [_inbox(uploader)[0].id]

    def test_a_broken_queue_does_not_break_the_upload_path(
        self, uploader: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """broker 掛掉時，`enqueue_*` 記 log 後回 None（1B 的既有約定）——
        通知寄不出去是一件小事，文件狀態寫不下去才是大事。"""
        import services.platform.notifications as module

        def explode(**_: Any) -> None:
            raise OSError("broker 連不上")

        monkeypatch.setattr(module, "enqueue_notification_email", explode)
        document_id = _upload(uploader)

        IngestionService().mark_retries_exhausted(
            TENANT_A, document_id, OSError("物件儲存連不上"), attempts=3
        )

        from apps.knowledge.models import Document

        with tenant_scope(TENANT_A):
            assert Document.objects.get(id=document_id).status == "failed"
        assert len(_inbox(uploader)) == 1

    def test_only_the_ready_notification_stays_off_the_wire(
        self, uploader: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`document.ready` 只走 in-app（見 tests/unit/test_notification_channels.py）
        ——批次上傳完成不該變成一批信。"""
        import services.platform.notifications as module

        sent: list[uuid.UUID] = []
        monkeypatch.setattr(
            module,
            "enqueue_notification_email",
            lambda *, tenant_id, notification_id: sent.append(notification_id),
        )
        document_id = _upload(uploader)
        IngestionService().ingest(TENANT_A, document_id)
        EmbeddingService(gateway=AIGateway(embedding_provider=_Provider())).embed_document(
            TENANT_A, document_id
        )

        assert sent == []
