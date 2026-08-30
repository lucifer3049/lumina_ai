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
    # **現有的 chunk 是用哪一版設定切出來的**（2B-6）。`knowledge_version` 在使用者
    # 按下儲存的那一刻就跳了，而 chunk 要等重建跑完才換——兩者相等才代表「不需要
    # 重建」。少了這一欄，「需要重建嗎」無從判定：唯一的替代是拿 chunk 的內容去比
    # 參數，而那既算不出來也不便宜。
    indexed_knowledge_version = models.IntegerField(default=1)
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
    # 上傳者（2A-5）。**可為 NULL**：三步走的第一步，且 `source_type` 不是 upload
    # 的來源（database / web 同步）本來就沒有「人」。裸 UUID 不是 FK，理由同
    # `platform.UsageLog.user_id`——刪掉一個使用者不該被他傳過的文件擋住。
    # 通知（08 §6 的 DLQ、ready）以它決定收件人；NULL 時退回租戶的 owner/admin。
    uploaded_by = models.UUIDField(null=True, blank=True)
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

    **全文檢索索引於 2B-1 落地**（`0006_chunk_fts_index`）：pgroonga 直接建在
    ``content`` 上，不需要 05 §5.3 原本寫的 ``content_tsv`` generated 欄位——那等於把
    同一份文字存兩次，且每次 re-ingest 都要重算。索引是多欄位
    ``(tenant_id, kb_id, content)`` 且 partial（``superseded = false``），理由見該
    migration 的 docstring。
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

    維度寫死 1024 是因為 migration 需要字面值，而它必須與 `ai_embedding_dimensions`
    一致（`tests/integration/test_embeddings.py` 對帳）。換 embedding 模型時兩者要一起改。

    **1536 → 1024 是 W1 做的一次不可逆全庫重建**（`0009_embedding_1024`，模型
    `BAAI/bge-m3`）。05 §5.6／06 §2.2 的「新版本算完 → 原子切換 → 清理舊版」是為**換
    模型**寫的，它默默假設了維度不變：上面那個唯一鍵讓兩版並存，而 halfvec 是固定寬度
    的欄位型別——1024 與 1536 塞不進同一欄，所以並存在換維度時不成立。下一次再換維度
    仍然是同一種代價：清空、改欄位、重建（重建走 2B-6 的 KB reindex）。

    ``deleted_at`` 繼承自 `TimestampedModel` 但**沒有軟刪除流程**：版本化資料不可變
    （05 §1），舊版本由清理 job 硬刪。欄位留著只為讓所有表形狀一致。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="embeddings")
    chunk = models.ForeignKey(Chunk, on_delete=models.PROTECT, related_name="embeddings")
    # provider 回報的實際模型（含別名解析後的版本），不是請求時寫的那個字串。
    model = models.TextField()
    embedding_version = models.IntegerField(default=1)
    vector = HalfVectorField(dimensions=1024)

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


class KbReindexJob(TimestampedModel):
    """KB 級重建的執行紀錄（06 §2.2 的四步，2B-6）。

    **與 `EtlJob` 分開**：那一張的粒度是「一份文件的一個階段」，冪等鍵是
    ``(document, doc_version, stage)``。重建的粒度是整個 KB，而它要回答的是
    ``EtlJob`` 答不出來的三件事：這次的**目標**是什麼（原子切換要切到哪裡去）、
    **完成度**多少（第 3 步的閘門）、**什麼時候切換的**（第 4 步的保留窗起點）。
    硬塞進 `EtlJob` 的話，這三個值只能靠掃全表推回來——而第 3 步是不可逆的。

    ``target_*`` 三欄就是 06 §2.2 第 1 步的「設定新 model/version」**存放處**：
    存進 KB 自己的欄位就是第 3 步提前發生（KB 只有一組現行值），檢索會在新向量算完
    之前就照一個一列都對不上的 ``(model, version)`` 去查，整庫回零筆而 API 全部 200。
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="kb_reindex_jobs")
    kb = models.ForeignKey(KnowledgeBase, on_delete=models.PROTECT, related_name="reindex_jobs")
    # 目標（第 1 步）。`target_embedding_version` 必定大於 KB 現行值——兩版並存靠的
    # 就是 `Embedding` 的 `UNIQUE(chunk, model, embedding_version)`。
    target_model = models.TextField()
    target_embedding_version = models.IntegerField()
    target_knowledge_version = models.IntegerField()
    # 這次要不要連 chunk 一起重切（切塊參數變了）。
    rechunk = models.BooleanField(default=False)
    # pending / rechunking / embedding / completed / failed
    # （`services/knowledge/reindex_plan.py` 是這組字串的單一來源）
    status = models.TextField(default="pending")
    # 進度。分母在開跑時定下來——邊跑邊算的話，進度會隨新上傳的文件倒退。
    total_chunks = models.IntegerField(default=0)
    embedded_chunks = models.IntegerField(default=0)
    total_documents = models.IntegerField(default=0)
    rechunked_documents = models.IntegerField(default=0)
    # 重切進行到哪一份文件（依 id 排序的游標）。**不用「已處理筆數」當偏移量**：
    # 重建期間有人刪掉或上傳一份文件，整個視窗就位移一格，而被跳過的那份會安靜地
    # 留著舊參數切出來的 chunk。游標比對的是 id，增刪都不影響。
    rechunk_cursor = models.UUIDField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    # **第 4 步的保留窗從這裡起算**，不是從 `created_at`：可回退的窗口是從切換那一刻
    # 開始的，用建立時間算等於「重建跑得愈久、可回退的時間愈短」——而跑得久的正是
    # 最該留退路的那些。
    switched_at = models.DateTimeField(null=True, blank=True)
    # 舊版向量已清（第 4 步做完）。清理器靠它跳過已處理的 job，不必每天重掃一次。
    purged_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            # **同一個 KB 同時只能有一個進行中的 job**。約束而不只是 service 的 if：
            # 使用者在等 40 分鐘時會再按一次（那是預期行為），而兩個請求會同時通過
            # 那個 if——接著兩個 job 各自往同一批 chunk 寫不同版本的向量，然後互相
            # 把對方切掉。條件與 `reindex_plan.REINDEX_ACTIVE_STATUSES` 是同一份。
            models.UniqueConstraint(
                fields=["kb"],
                condition=models.Q(status__in=["pending", "rechunking", "embedding"]),
                name="uq_reindex_job_active_per_kb",
            ),
        ]
        indexes = [
            # 「這個 KB 最近一次重建」——進度端點走這條。
            models.Index(fields=["tenant", "kb", "-created_at"], name="ix_reindex_tenant_kb_time"),
            # 第 4 步的清理器：切換過、還沒清的那些。
            models.Index(fields=["tenant", "status"], name="ix_reindex_tenant_status"),
        ]

    def __str__(self) -> str:
        return f"KbReindexJob({self.kb_id}→{self.target_model}v{self.target_embedding_version})"


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
