"""Knowledge 的 factory（apps/knowledge/models.py）。

形狀與 `tests/factories/identity.py` 一致：factory 類別關在模組內，對外只給有回傳
型別的 `make_*` 薄包裝（理由見該檔——factory_boy 的 metaclass 讓 mypy strict 在每個
呼叫點報「呼叫未標型別的函式」）。

**建立資料一律要在租戶 context ＋ 交易內**：四張表都有 RLS，`WITH CHECK` 讀的是
交易區域參數 `app.tenant_id`。沿用 identity 那份 `tenant_scope`，不另做一個——
兩份會漂，而漂掉時症狀是「某些測試的 INSERT 被 policy 擋掉」而看不出為什麼。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, cast

import factory

from apps.identity.models import Tenant
from apps.knowledge.models import Chunk, Document, Embedding, EtlJob, KnowledgeBase
from config.settings.app_settings import get_app_settings


class KnowledgeBaseFactory(factory.django.DjangoModelFactory[KnowledgeBase]):
    class Meta:
        model = KnowledgeBase

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory("tests.factories.identity.TenantFactory")
    name = factory.Sequence(lambda n: f"KB {n}")
    description = ""
    # chunk 策略與檢索參數的預設值（05 §3.2 的 config jb）。空 dict 代表「全部用
    # 系統預設」，那是 1B 唯一支援的形狀；策略切換屬 1B-5 的 chunker。
    config: dict[str, object] = {}
    embedding_model = "text-embedding-3-small"
    embedding_version = 1
    knowledge_version = 1
    status = "active"


class DocumentFactory(factory.django.DjangoModelFactory[Document]):
    class Meta:
        model = Document

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SelfAttribute("kb.tenant")
    kb = factory.SubFactory(KnowledgeBaseFactory)
    filename = factory.Sequence(lambda n: f"doc-{n}.pdf")
    mime_type = "application/pdf"
    # storage_key 的形狀出自 05 §3.2：tenant-{slug}/kb/{kb_id}/{doc_id}。
    # 這裡用 LazyAttribute 而非固定字串，才驗得到「兩份文件不會指向同一個物件」。
    # 走 ``doc.kb.id`` 而非 ``doc.kb_id``：LazyAttribute 拿到的是 factory 的
    # **建構中 stub**，上面只有宣告過的屬性；``kb_id`` 是 model 欄位、stub 上不存在
    # （症狀是 AttributeError: The parameter 'kb_id' is unknown）。
    storage_key = factory.LazyAttribute(
        lambda doc: f"tenant-{doc.tenant.slug}/kb/{doc.kb.id}/{doc.id}"
    )
    # content_hash 跟著 filename 走：預設情況下每份文件的 hash 都不同（否則
    # UNIQUE(tenant_id, kb_id, content_hash) 會在無關的測試裡誤觸發），而要驗去重的
    # 測試顯式傳同一個值。
    content_hash = factory.LazyAttribute(
        lambda doc: hashlib.sha256(str(doc.filename).encode()).hexdigest()
    )
    size_bytes = 1024
    source_type = "upload"
    source_meta: dict[str, object] = {}
    status = "uploaded"
    doc_version = 1
    error: dict[str, object] | None = None


class ChunkFactory(factory.django.DjangoModelFactory[Chunk]):
    class Meta:
        model = Chunk

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SelfAttribute("document.tenant")
    document = factory.SubFactory(DocumentFactory)
    # kb 是反正規化欄位（05 §3.2：檢索免 join）——跟著 document 走，不獨立指定，
    # 否則測試可以造出「chunk 的 kb 與其 document 的 kb 不同」這種不可能的資料。
    kb = factory.SelfAttribute("document.kb")
    seq = factory.Sequence(lambda n: n)
    content = factory.Sequence(lambda n: f"chunk content {n}")
    token_count = 16
    chunk_version = 1
    doc_version = factory.SelfAttribute("document.doc_version")
    meta: dict[str, object] = {}
    superseded = False


class EmbeddingFactory(factory.django.DjangoModelFactory[Embedding]):
    class Meta:
        model = Embedding

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SelfAttribute("chunk.tenant")
    chunk = factory.SubFactory(ChunkFactory)
    model = "mock-embedding"
    embedding_version = 1
    # 維度跟著設定走（見 apps/knowledge/models.py 的說明）：寫死在這裡的話，換模型時
    # 每個測試都會以「expected N dimensions」失敗，而錯誤訊息指向 INSERT。
    vector = factory.LazyFunction(lambda: [0.1] * get_app_settings().ai_embedding_dimensions)


class EtlJobFactory(factory.django.DjangoModelFactory[EtlJob]):
    class Meta:
        model = EtlJob

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SelfAttribute("document.tenant")
    document = factory.SubFactory(DocumentFactory)
    stage = "extract"
    status = "pending"
    attempt = 0
    stats: dict[str, object] = {}
    error: dict[str, object] | None = None
    celery_task_id = ""


# ── 對外介面：有回傳型別的薄包裝 ────────────────────────────────


def _resolve(kwargs: dict[str, Any]) -> dict[str, Any]:
    """把 ``*_id=<uuid>`` 換成對應的實例（理由見 identity 的 `_with_tenant`）。

    ``kb_id`` / ``document_id`` 也要處理，不只 ``tenant_id``：factory 不認得的關鍵字
    會被當成未指定，於是 `SubFactory` **另外造一個** KB（連帶一個新租戶）——而建立
    租戶會被 RLS 擋下，錯誤訊息是 ``new row violates row-level security policy for
    table "identity_tenant"``，完全看不出根因是參數名寫成了 ``kb_id``。
    """
    for keyword, model in (
        ("tenant_id", Tenant),
        ("kb_id", KnowledgeBase),
        ("document_id", Document),
        ("chunk_id", Chunk),
    ):
        value = kwargs.pop(keyword, None)
        if value is not None:
            kwargs[keyword.removesuffix("_id")] = model.objects.get(id=value)
    return kwargs


def make_knowledge_base(**kwargs: Any) -> KnowledgeBase:
    return cast(KnowledgeBase, KnowledgeBaseFactory(**_resolve(kwargs)))


def make_document(**kwargs: Any) -> Document:
    return cast(Document, DocumentFactory(**_resolve(kwargs)))


def make_chunk(**kwargs: Any) -> Chunk:
    return cast(Chunk, ChunkFactory(**_resolve(kwargs)))


def make_embedding(**kwargs: Any) -> Embedding:
    return cast(Embedding, EmbeddingFactory(**_resolve(kwargs)))


def make_etl_job(**kwargs: Any) -> EtlJob:
    return cast(EtlJob, EtlJobFactory(**_resolve(kwargs)))
