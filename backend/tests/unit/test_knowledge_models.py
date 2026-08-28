"""驗收：Knowledge Model 的形狀（05 §3.2、§4、§5.4；CLAUDE.md 鐵則 6）。

與 `test_identity_models.py` 同一個分工：本檔只看 Django 的 model metadata，不碰
資料庫——這些是**宣告**對不對的問題，不是行為問題。RLS 的實際行為由
`tests/integration/test_rls_knowledge.py` 驗。

四張表在 1B-1 一起建（`embeddings` 屬 1C）。四張都含租戶資料，所以四張都要有
tenant_id、都要開 RLS——**表建好但 policy 沒跟上**是最貴的一種漏做：等到有資料
之後才補，得先確認現存資料沒有跨租戶污染，而那時已經無從確認。

本檔特別盯的是三個「小資料量下測不出來」的宣告：

1. ``UNIQUE(tenant_id, kb_id, content_hash)``——去重。寫成全域唯一的話，兩個租戶
   上傳同一份公開 PDF，第二個租戶會被擋下並看到「文件已存在」，而他根本沒上傳過。
2. ``chunks`` 的 partial index ``WHERE NOT superseded``——檢索候選集。少了 partial
   條件，舊版本的 chunk 會一直留在索引裡，索引大小隨 re-ingest 次數線性成長。
3. FK 不設 CASCADE（05 §5.4）——刪除走顯式的分批清理 worker。設了 CASCADE 的話，
   刪一個 KB 會在一個交易內連鎖刪掉數十萬列 chunk，鎖表且 vector index 抖動。
"""

from __future__ import annotations

import inspect

from django.db import models

from apps.knowledge.models import Chunk, Document, EtlJob, KbReindexJob, KnowledgeBase

ALL_KNOWLEDGE_MODELS = (KnowledgeBase, Document, Chunk, EtlJob)

# 四張表全部是 tenant-scoped（沒有 identity 那種全域字典表的例外）。
TENANT_SCOPED_MODELS = ALL_KNOWLEDGE_MODELS

# 05 §5.4：軟刪除只適用「使用者可能後悔」的實體。KB 與 document 在列，
# chunk 與 etl_job 不在（它們隨 document 走，自己沒有獨立的復原語意）。
SOFT_DELETABLE_MODELS = (KnowledgeBase, Document)

_ALLOWED_METHOD_NAMES = frozenset({"__str__"})


def _own_methods(model: type[models.Model]) -> set[str]:
    return {
        name
        for name, member in vars(model).items()
        if inspect.isfunction(member) and name not in _ALLOWED_METHOD_NAMES
    }


def _field_names(model: type[models.Model]) -> set[str]:
    return {field.attname for field in model._meta.get_fields() if hasattr(field, "attname")}


def _unique_field_tuples(model: type[models.Model]) -> set[tuple[str, ...]]:
    """model 上宣告的所有唯一組合（`unique_together` 與 `UniqueConstraint` 兩種寫法）。"""
    pairs = {tuple(fields) for fields in model._meta.unique_together}
    pairs |= {
        tuple(constraint.fields)
        for constraint in model._meta.constraints
        if isinstance(constraint, models.UniqueConstraint) and constraint.fields
    }
    return pairs


def test_models_stay_thin() -> None:
    """model 只有欄位、Meta、``__str__``（鐵則 6）。

    在 knowledge 這幾張表上，違規的形狀很好想像：``document.rebuild_chunks()`` 或
    ``kb.search(query)``。兩者都會從 ``self`` 出發直接查關聯資料，那條路徑完全不經過
    Repository 的 tenant filter，而且會把 ETL / 檢索邏輯散進 model 層。
    """
    offenders = {model.__name__: _own_methods(model) for model in ALL_KNOWLEDGE_MODELS}
    offenders = {name: methods for name, methods in offenders.items() if methods}

    assert not offenders, (
        f"model 上出現業務方法：{offenders}——業務規則放 Service、查詢放 Repository（鐵則 6）"
    )


def test_every_model_has_a_tenant_column() -> None:
    """四張表全部要有 tenant_id——沒有它就沒有東西可供 RLS policy 比對。

    ``chunks`` 與 ``etl_jobs`` 的 tenant_id 是**刻意的冗餘**（它們的租戶其實可以從
    document join 回去）。理由與 identity 的 ``user_roles`` 相同：讓 policy 條件保持
    單純，不讓這張表的隔離正確性依賴另一張表的 policy。
    """
    missing = [
        model.__name__ for model in TENANT_SCOPED_MODELS if "tenant_id" not in _field_names(model)
    ]

    assert not missing, f"缺 tenant_id 欄位：{missing}"


def test_chunks_denormalise_kb_id() -> None:
    """``chunks.kb_id`` 是反正規化欄位（05 §3.2：檢索免 join）。

    檢索的熱路徑是「這個 KB 底下、未 superseded 的 chunk」。少了這欄就得
    join documents 才能過濾 KB，而那是每次查詢都要付的成本。
    """
    assert "kb_id" in _field_names(Chunk), "chunks 缺 kb_id（檢索要 join documents 才能過濾 KB）"


def test_documents_dedupe_within_a_kb_not_globally() -> None:
    """``UNIQUE(tenant_id, kb_id, content_hash)``（05 §3.2、§4）。

    三個欄位缺一不可：
    - 少 tenant_id → 跨租戶誤判重複（別的公司上傳過同一份公開文件就擋下你）。
    - 少 kb_id → 同一份文件不能同時放進「法規」與「教育訓練」兩個 KB，而那是
      正當需求。
    """
    unique_tuples = _unique_field_tuples(Document)
    expected = {("tenant", "kb", "content_hash"), ("tenant_id", "kb_id", "content_hash")}

    assert unique_tuples & expected, (
        f"documents 缺 UNIQUE(tenant_id, kb_id, content_hash)，現有：{sorted(unique_tuples)}"
    )

    content_hash = Document._meta.get_field("content_hash")
    assert not getattr(content_hash, "unique", False), (
        "content_hash 被宣告為全域唯一——同一份文件本來就可能出現在不同租戶／不同 KB"
    )


def test_documents_have_the_listing_index() -> None:
    """``(tenant_id, kb_id, status)`` —— 05 §4 指名的列表查詢。

    文件列表永遠是「某個 KB 底下的文件，通常再按狀態過濾（處理中／失敗）」。
    """
    index_fields = {tuple(index.fields) for index in Document._meta.indexes}
    expected = {("tenant", "kb", "status"), ("tenant_id", "kb_id", "status")}

    assert index_fields & expected, (
        f"documents 缺 (tenant_id, kb_id, status) 索引，現有：{sorted(index_fields)}"
    )


def test_chunks_retrieval_index_excludes_superseded_rows() -> None:
    """``chunks`` 的檢索索引必須是 partial（``WHERE NOT superseded``，05 §4）。

    superseded 的 chunk 是舊版本的殘留，檢索永遠不會要它們。少了 partial 條件，
    索引會把每一次 re-ingest 的歷史版本都收進去——索引大小隨重跑次數線性成長，
    而查詢結果完全正確，所以不會有任何人發現。
    """
    partial_indexes = [
        index
        for index in Chunk._meta.indexes
        if index.condition is not None
        and tuple(index.fields)[:2] in {("tenant", "kb"), ("tenant_id", "kb_id")}
    ]

    assert partial_indexes, (
        "chunks 缺 (tenant_id, kb_id) WHERE NOT superseded 的 partial index（05 §4）——"
        f"現有索引：{[(tuple(i.fields), i.condition) for i in Chunk._meta.indexes]}"
    )


def test_composite_indexes_lead_with_tenant_id() -> None:
    """複合索引首欄一律 tenant_id（05 §2）。

    首欄不是 tenant_id 的索引在「先過濾租戶」的查詢裡幾乎用不到。開發環境看不出來
    （每租戶幾十列），要等租戶數上百、單一租戶只佔總量千分之幾時才顯現。
    """
    offenders = []
    for model in TENANT_SCOPED_MODELS:
        for index in model._meta.indexes:
            first = index.fields[0].lstrip("-")
            if len(index.fields) > 1 and first not in ("tenant", "tenant_id"):
                offenders.append(f"{model.__name__}.{index.name}: {index.fields}")

    assert not offenders, f"複合索引首欄不是 tenant_id：{offenders}"


def test_foreign_keys_never_cascade() -> None:
    """FK 一律不設 ``CASCADE``（05 §5.4）。

    刪除走顯式的分批清理 worker：embeddings → chunks → documents，分批提交。
    設 CASCADE 的話，刪一個 KB 會在單一交易內連鎖刪掉數十萬列 chunk 與 embedding
    ——鎖表、WAL 暴增、vector index 抖動，而且中途失敗就是全部回滾（等於刪不掉）。
    """
    offenders = []
    for model in ALL_KNOWLEDGE_MODELS:
        for field in model._meta.get_fields():
            on_delete = getattr(getattr(field, "remote_field", None), "on_delete", None)
            if on_delete is models.CASCADE:
                offenders.append(f"{model.__name__}.{field.name}")

    assert not offenders, f"以下 FK 設了 CASCADE（05 §5.4 要求顯式分批刪）：{offenders}"


def test_soft_deletable_models_have_deleted_at() -> None:
    """KB 與 document 支援軟刪除（05 §5.4：使用者可能後悔的實體，30 天後硬刪）。"""
    missing = [
        model.__name__ for model in SOFT_DELETABLE_MODELS if "deleted_at" not in _field_names(model)
    ]

    assert not missing, f"缺 deleted_at（軟刪除）：{missing}"


def test_document_carries_the_state_machine_fields() -> None:
    """08 §2 狀態機需要的三個欄位：``status``、``doc_version``、``error``。

    ``doc_version`` 是冪等鍵 ``(doc_id, doc_version, stage)`` 的一部分（08 §6），
    少了它，re-ingest 的重跑無法與上一版的殘留區分——舊 chunk 會與新 chunk 混在一起
    被檢索到，而兩者內容都「正確」，只是不該同時存在。
    """
    fields = _field_names(Document)

    for name in ("status", "doc_version", "error"):
        assert name in fields, f"documents 缺 {name}（08 §2 狀態機 / §6 冪等鍵）"


def test_etl_job_carries_the_idempotency_key_fields() -> None:
    """``etl_jobs`` 要能回答「這個 (doc, version, stage) 跑過了嗎」（08 §6）。

    ``attempt`` 也在內：retry ≤3 的上限要有地方記，記在 Celery 的 task meta 裡
    是不夠的——那份資料會過期，而「這份文件重試幾次了」是使用者要看的東西。
    """
    fields = _field_names(EtlJob)

    for name in ("document_id", "stage", "status", "attempt"):
        assert name in fields, f"etl_jobs 缺 {name}（08 §6 冪等與重試）"


def test_tables_use_djangos_default_names() -> None:
    """表名一律用 Django 預設（``<app_label>_<model 小寫>``），不設 ``db_table``。

    2026-08-09 的決定。表名是 RLS migration 與日後所有維運 SQL 的字面依賴，因此要
    有一條規則、而且只有一條——「不設就對了」比「每張表想一個 snake_case 名字」更難
    寫錯，也不會有人漏設。

    代價要誠實記著：identity 那組對多字模型設了 snake_case 的 ``db_table``
    （``identity_user_role``），與這裡的 ``knowledge_etljob`` 風格不一致。統一要動
    identity 的表名（rename migration），成本遠高於收益，故不做。
    """
    expected = {
        KnowledgeBase: "knowledge_knowledgebase",
        Document: "knowledge_document",
        Chunk: "knowledge_chunk",
        EtlJob: "knowledge_etljob",
        KbReindexJob: "knowledge_kbreindexjob",
    }

    actual = {model: model._meta.db_table for model in expected}

    assert actual == expected, f"表名不是 Django 預設：{actual}"
