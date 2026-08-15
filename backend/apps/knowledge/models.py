"""Knowledge 的資料模型（05 §3.2）。

鐵則 6：model 只有欄位、Meta、``__str__``。在這幾張表上，違規的形狀很好想像
——``document.rebuild_chunks()`` 或 ``kb.search(query)``：兩者都會從 ``self`` 出發
直接查關聯資料，那條路徑完全不經過 Repository 的 tenant filter，而且會把 ETL 與
檢索邏輯散進 model 層。

**四張表全部有 tenant_id，四張全部開 RLS**（migration 0002）。`Chunk` 與 `EtlJob`
的 tenant 是**刻意的冗餘**——它們的租戶其實可以從 `Document` join 回去。理由與
identity 的 `UserRole` 相同：讓 policy 條件保持單純，不讓這張表的隔離正確性依賴
另一張表的 policy。多一欄換「出事時推理得動」。

`Chunk` 的 ``kb`` 是另一種反正規化，目的不同：檢索的熱路徑是「這個 KB 底下、未
superseded 的 chunk」，少了這欄每次查詢都要 join documents。

**表名用 Django 預設**（不設 ``db_table``）：``knowledge_knowledgebase``、
``knowledge_etljob``。identity 那組對多字模型設了 snake_case 的 ``db_table``
（``identity_user_role``），兩邊因此不一致——這是明確的決定（2026-08-09），代價是
命名風格分歧，換到的是「不必為每張新表想一次表名，也不會有人漏設」。

FK 一律不設 CASCADE（05 §5.4）：刪除走顯式的分批清理 worker（embeddings → chunks
→ documents，分批提交）。設了 CASCADE 的話，刪一個 KB 會在單一交易內連鎖刪掉數十
萬列——鎖表、WAL 暴增、vector index 抖動，中途失敗還會全部回滾（等於刪不掉）。
"""

from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import HalfVectorField, HnswIndex

from apps.identity.models import Tenant


class TimestampedModel(models.Model):
    """標配三欄（05 §3 前言）：建立、更新、軟刪除時間。

    與 `apps.identity.models.TimestampedModel` 內容相同但**刻意各自宣告一份**：
    抽象基底放在 identity 底下，會讓 knowledge 為了一個共用欄位組而依賴另一個
    bounded context（ADR-006）。共用的正確位置是一個共同的基礎模組，那需要動
    identity（本次禁區），因此記在這裡待日後一併處理。

    ``deleted_at`` 對 `Chunk` / `EtlJob` 沒有使用者可觸發的軟刪除流程（05 §5.4 只
    涵蓋 KB / document / conversation / prompt），但欄位先留著讓所有表形狀一致
    ——事後加欄位到大表要走三步走遷移。
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class KnowledgeBase(TimestampedModel):
    """知識庫——文件的容器，也是檢索與 chunk 策略的設定單位。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="knowledge_bases")
    name = models.TextField()
    description = models.TextField(default="", blank=True)
    # chunk 策略與檢索參數的預設值（05 §3.2）。空 dict = 全部走系統預設，
    # 那是 1B 唯一支援的形狀；策略切換屬 1B-5 的 chunker。
    config = models.JSONField(default=dict, blank=True)
    embedding_model = models.TextField(default="")
    embedding_version = models.IntegerField(default=1)
    # 設定變更（chunk 策略、embedding 模型）時遞增，供「這個 KB 需要重建嗎」判定。
    knowledge_version = models.IntegerField(default=1)
    status = models.TextField(default="active")

    class Meta:
        indexes = [
            models.Index(fields=["tenant", "status"], name="ix_kb_tenant_status"),
        ]

    def __str__(self) -> str:
        return f"KnowledgeBase({self.name})"


class Document(TimestampedModel):
    """一份文件的中繼資料；原檔在物件儲存，chunk 在 `Chunk`。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="documents")
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.PROTECT, related_name="documents")
    filename = models.TextField()
    # MIME 白名單在應用層驗證（05 §3.2）——存進來的是實際偵測到的型別。
    mime_type = models.TextField()
    # MinIO object key：tenant-{slug}/kb/{kb_id}/{doc_id}（05 §3.2）
    storage_key = models.TextField()
    # SHA-256。去重鍵的一部分，見 Meta.constraints。
    content_hash = models.TextField()
    size_bytes = models.BigIntegerField()
    source_type = models.TextField(default="upload")
    source_meta = models.JSONField(default=dict, blank=True)
    # 08 §2 狀態機：uploaded → parsing → chunked → embedding → ready / failed
    status = models.TextField(default="uploaded")
    # re-ingest 時遞增；冪等鍵 (doc_id, doc_version, stage) 的一部分（08 §6）。
    doc_version = models.IntegerField(default=1)
    # 結構化失敗原因（stage、可否重跑）。null 代表沒有失敗過。
    error = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            # 三個欄位缺一不可（05 §3.2、§4）：
            # 少 tenant → 別的租戶上傳過同一份公開文件就把你擋下；
            # 少 kb → 同一份文件不能同時放進「法規」與「新人訓練」兩個 KB。
            models.UniqueConstraint(
                fields=["tenant", "kb", "content_hash"],
                name="uq_document_tenant_kb_hash",
            ),
        ]
        indexes = [
            # 05 §4 指名的列表查詢：某個 KB 底下的文件，通常再按狀態過濾。
            models.Index(fields=["tenant", "kb", "status"], name="ix_doc_tenant_kb_status"),
        ]

    def __str__(self) -> str:
        return f"Document({self.filename})"


class Chunk(TimestampedModel):
    """切塊後的片段——檢索實際讀的那張表。

    ``content_tsv``（pgroonga 全文檢索索引，05 §5.3）不在 1B-1：先寫 query pattern
    再開 index（05 §4 原則），而查詢要到 1C 檢索才存在。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="chunks")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="chunks")
    # 反正規化（05 §3.2）：檢索免 join documents。
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.PROTECT, related_name="chunks")
    seq = models.IntegerField()
    content = models.TextField()
    token_count = models.IntegerField(default=0)
    # chunk 策略版本；策略改變時重切，舊的標 superseded。
    chunk_version = models.IntegerField(default=1)
    doc_version = models.IntegerField(default=1)
    # 頁碼、標題路徑、表格標記（08 §3 的 ExtractedDoc block meta）
    meta = models.JSONField(default=dict, blank=True)
    # 新版本產生後標記；重嵌入完成後由清理 job 硬刪（05 §3.2）。
    superseded = models.BooleanField(default=False)

    class Meta:
        constraints = [
            # 同一份文件的同一版本內，seq 不得重複——重跑時「先刪同版本殘留再寫入」
            # （08 §6）若漏刪，症狀是同一段內容在檢索結果裡出現兩次。
            models.UniqueConstraint(
                fields=["document", "doc_version", "seq"],
                name="uq_chunk_document_version_seq",
            ),
        ]
        indexes = [
            # 檢索候選集（05 §4）。**partial 是重點**：superseded 的列是舊版本殘留，
            # 檢索永遠不會要它們。少了條件，索引會把每次 re-ingest 的歷史版本都收進去
            # ——大小隨重跑次數線性成長，而查詢結果完全正確，所以沒有人會發現。
            models.Index(
                fields=["tenant", "kb"],
                condition=models.Q(superseded=False),
                name="ix_chunk_tenant_kb_active",
            ),
            # 文件預覽與 chunk 拼接：按 seq 取回一份文件的全部片段。
            models.Index(fields=["tenant", "document", "seq"], name="ix_chunk_tenant_doc_seq"),
        ]

    def __str__(self) -> str:
        return f"Chunk({self.document_id}#{self.seq})"


class Embedding(TimestampedModel):
    """chunk 的向量（05 §3.2）——檢索實際比對的東西。

    **一個 chunk 可以有多份向量**：`(chunk, model, embedding_version)` 是唯一鍵，而
    不是 `chunk` 本身。重嵌入的做法是「新版本算完 → 原子切換 → 清理舊版」（06 §2.2），
    那需要兩個版本並存的那段時間；約束若只有 chunk，切換期間就得先刪再寫，而那幾分鐘
    檢索會查不到任何東西。

    ``vector`` 是 **halfvec**（fp16）而不是 vector（fp32）：儲存與記憶體減半，召回
    差異依 pgvector 實測可忽略（05 §3.2 的 F-01 決議，Phase 2 golden set 覆核）。
    代價是精度只到小數點後三位左右——寫入時 pgvector 自動轉換，讀回來的值會與寫進去的
    略有差異，那是預期行為。

    維度寫死 1536 是因為 migration 需要字面值，而它必須與 `ai_embedding_dimensions`
    一致（`tests/integration/test_embeddings.py` 對帳）。換 embedding 模型時兩者要一起
    改，並走 05 §5.6 的遷移流程——維度不同的向量無法共存於同一欄。

    ``deleted_at`` 繼承自 `TimestampedModel` 但**沒有軟刪除流程**：版本化資料不可變
    （05 §1），舊版本由清理 job 硬刪。欄位留著只為讓所有表形狀一致。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="embeddings")
    chunk = models.ForeignKey(Chunk, on_delete=models.PROTECT, related_name="embeddings")
    # provider 回報的實際模型（含別名解析後的版本），不是請求時寫的那個字串。
    model = models.TextField()
    embedding_version = models.IntegerField(default=1)
    vector = HalfVectorField(dimensions=1536)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "model", "embedding_version"],
                name="uq_embedding_chunk_model_version",
            ),
        ]
        indexes = [
            # 05 §4：HNSW + **halfvec** 的 ops。ops 與欄位型別不合時不會報錯，
            # 索引只是永遠不會被選用——檢索從數十毫秒退化成整表掃描，而結果正確。
            # m / ef_construction 取 05 §4 的值；建索引吃 maintenance_work_mem（§5.5）。
            HnswIndex(
                name="ix_embedding_vector_hnsw",
                fields=["vector"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            ),
            # 「這批 chunk 哪些已經有向量」——1C-3 的批次依據走這條。
            models.Index(fields=["tenant", "chunk"], name="ix_embedding_tenant_chunk"),
        ]

    def __str__(self) -> str:
        return f"Embedding({self.chunk_id}/{self.model}v{self.embedding_version})"


class EtlJob(TimestampedModel):
    """ETL 的階段執行紀錄——冪等與重試都靠它（08 §6）。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="etl_jobs")
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="etl_jobs")
    # **05 §3.2 的欄位表沒有這一欄，這是刻意的偏離（2026-08-09 決定）。**
    # 08 §6 的冪等鍵是 (doc_id, doc_version, stage)，而只有 document_id 的話，
    # 「這個階段跑過了嗎」得 join documents 取當下的 doc_version——那個值會隨
    # re-ingest 改變，於是新版本會查到上一版**已成功**的 job 而被判定為跑過，
    # 文件停在舊內容、狀態卻是 ready。把版本固定在 job 自己身上才答得準。
    doc_version = models.IntegerField(default=1)
    # extract / clean / chunk / embed（08 §2）
    stage = models.TextField()
    # pending / running / succeeded / failed / retrying
    status = models.TextField(default="pending")
    # retry ≤3 的計數（08 §6）。不記在 Celery task meta 裡：那份資料會過期，
    # 而「這份文件重試幾次了」是使用者要看得到的東西。
    attempt = models.IntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # 頁數、block 數、丟棄率、chunk 數、平均 token（08 §4）
    stats = models.JSONField(default=dict, blank=True)
    error = models.JSONField(null=True, blank=True)
    celery_task_id = models.TextField(default="", blank=True)

    class Meta:
        constraints = [
            # 冪等鍵。約束而不只是查詢慣例：併發觸發（使用者連點兩次 reingest、
            # 或重試與排程同時進來）時，第二筆會被 DB 擋下而不是產生兩個各自
            # 執行同一階段的 job。
            models.UniqueConstraint(
                fields=["document", "doc_version", "stage"],
                name="uq_etl_job_document_version_stage",
            ),
        ]
        indexes = [
            # 「這個租戶有哪些卡住/失敗的 job」——維運與前端進度輪詢。
            models.Index(fields=["tenant", "status"], name="ix_etl_job_tenant_status"),
        ]

    def __str__(self) -> str:
        return f"EtlJob({self.document_id}/{self.stage})"
