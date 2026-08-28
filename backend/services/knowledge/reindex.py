"""KbReindexService —— KB 級重建的編排（06 §2.2 的四步、04 §4.4，2B-6）。

    1. KB 設定新 model/version（**舊版持續服務查詢**）
    2. 背景批次：對全部 active chunks 產生新版 embeddings（限速）
    3. 完成度 100% → KB.embedding_version 原子切換 → 查詢改用新版
    4. 觀察期（可回退）→ 清理 Job 刪舊版 embeddings（`OldEmbeddingCleanupService`）

**與 `EmbeddingService` 的分工**：那一支的單位是「一份文件」，由 ETL 觸發，模型與
版本一律取 KB 的**現行**值。這一支的單位是「一個 KB」，而它的全部意義就在於用**不是
現行值**的那一組去算——兩者混在一起的話，`embed_document` 得多一個「這次要算哪一版」
的參數，而那個參數在 99% 的呼叫裡都是 None。

**判斷全部在 `reindex_plan.py`**（純函式，無 DB）：要不要重建、下一步是什麼、可以
切換了嗎。這裡只負責「照著做」與交易邊界。

兩種 job 的形狀不同，值得先講清楚：

- **重嵌入**（換模型／重算）：既有 chunk 不動，用新的 ``(model, version)`` 另外算一
  份向量。並存期間檢索照舊走舊版，算完才原子切換。
- **重切**（chunk 參數變了）：逐份走既有的 `DocumentService.reingest`，產生的是全新
  的 chunk 列（舊的當場 superseded）。**版本號不遞增**——新 chunk 由正常的
  ETL→embedding 路徑算一次向量就夠了，遞增只會讓同一批 chunk 被算兩次（見
  `reindex_plan.plan_reindex` 的註解）。這一支在重切階段做的事是「排 re-ingest 並
  等它們回到 ready」，然後補算漏網的（enqueue 掉訊息、或 KB 裡本來就有沒向量的
  chunk），最後把 ``indexed_knowledge_version`` 切過去。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from ai.gateway import AIGateway, build_gateway
from config.logging import get_logger
from core import audit
from core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from core.tasks import enqueue_reindex
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    EmbeddingRepository,
    EmbeddingRow,
    KbReindexJobRepository,
    KnowledgeBaseRepository,
)
from services.knowledge.documents import DocumentService
from services.knowledge.embedding import EMBED_BATCH_SIZE, model_for
from services.knowledge.failures import error_payload
from services.knowledge.reindex_plan import (
    REINDEX_ACTIVE_STATUSES,
    STATUS_COMPLETED,
    STATUS_EMBEDDING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RECHUNKING,
    ReindexProgress,
    next_status,
    plan_reindex,
    ready_to_switch,
)
from services.platform.usage import UsageEvent, UsageService

logger = get_logger(__name__)

__all__ = ["KbReindexJobView", "KbReindexService"]

# 一次 advance 最多重切幾份文件。分批的理由與清理 job 相同：一個上千份文件的 KB
# 會讓單一次呼叫送出上千個 ETL 任務，而那些任務會把其他租戶的上傳擠到後面去。
RECHUNK_BATCH_SIZE = 50

# re-ingest 之後還在跑的狀態——重切階段要等它們全部離開這一組才進 embedding。
# ``failed`` 不在其中：壞檔會永遠停在那裡，而一份壞檔不該讓整個 KB 的重建卡死
# （它的舊 chunk 已經 superseded，本來就不在檢索裡）。
_STILL_PROCESSING = frozenset({"uploaded", "parsing", "cleaned", "chunked", "embedding"})


@dataclass(frozen=True)
class KbReindexJobView:
    id: uuid.UUID
    kb_id: uuid.UUID
    status: str
    target_model: str
    target_embedding_version: int
    target_knowledge_version: int
    rechunk: bool
    total_chunks: int
    embedded_chunks: int
    total_documents: int
    rechunked_documents: int
    started_at: datetime | None
    switched_at: datetime | None
    finished_at: datetime | None
    error: dict[str, Any] | None


class KbReindexService:
    def __init__(
        self,
        *,
        gateway: AIGateway | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        documents: DocumentRepository | None = None,
        chunks: ChunkRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        jobs: KbReindexJobRepository | None = None,
        usage: UsageService | None = None,
        document_service: DocumentService | None = None,
    ) -> None:
        # Gateway 惰性建立，理由同 `EmbeddingService`：建構 service 不該因為
        # provider 未實作而失敗——`start` 與 `latest` 根本不打 provider。
        self._gateway = gateway
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._documents = documents or DocumentRepository()
        self._chunks = chunks or ChunkRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._jobs = jobs or KbReindexJobRepository()
        self._usage = usage or UsageService()
        # 重切走既有的 re-ingest（冪等鍵、superseded 標記、失敗分類都在那裡）。
        # 另寫一份的話那三件事就有兩份實作，而它們遲早會漂。
        self._documents_service = document_service or DocumentService()

    @property
    def gateway(self) -> AIGateway:
        if self._gateway is None:
            self._gateway = build_gateway()
        return self._gateway

    # ── 第 1 步：建立目標 ──────────────────────────────────────

    def start(
        self,
        tenant_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        target_model: str | None = None,
        rechunk: bool | None = None,
    ) -> KbReindexJobView:
        """開一個重建 job（09 §2.3 的 ``POST /knowledge-bases/{id}/reindex``，202）。

        **不動 KB 的現行值**：目標存在 job 上（`KbReindexJob` 的 docstring）。存進
        KB 就是第 3 步提前發生，檢索會照一個一列都對不上的 ``(model, version)`` 去
        查，整庫回零筆而 API 全部 200。
        """
        with tenant_context(tenant_id), unit_of_work():
            kb = self._require(kb_id)
            try:
                plan = plan_reindex(
                    current_model=model_for(kb.embedding_model),
                    current_embedding_version=int(kb.embedding_version),
                    knowledge_version=int(kb.knowledge_version),
                    indexed_knowledge_version=int(kb.indexed_knowledge_version),
                    target_model=target_model,
                    rechunk=rechunk,
                )
            except ValueError as exc:
                # 純函式只知道「這組參數不成立」，HTTP 語意在這一層決定（422）。
                raise ValidationFailedError(str(exc)) from exc

            # 先查一次是為了讓常見情況拿到清楚的錯誤；**真正擋住併發的是下面的
            # 唯一約束**——使用者連點兩次時，兩個請求會同時通過這個 if。
            if (
                self._jobs.active_for_kb(kb_id, statuses=sorted(REINDEX_ACTIVE_STATUSES))
                is not None
            ):
                raise ConflictError("這個知識庫正在重建中")

            try:
                # savepoint：IntegrityError 會讓交易進入 aborted，不隔開的話外層
                # `unit_of_work` 在離開時才炸，而那時錯誤訊息與重建無關。
                with transaction.atomic():
                    job = self._jobs.create(
                        kb_id=kb_id,
                        target_model=plan.target_model,
                        target_embedding_version=plan.target_embedding_version,
                        target_knowledge_version=plan.target_knowledge_version,
                        rechunk=plan.rechunk,
                        total_chunks=self._chunks.count_active_for_kb(kb_id=kb_id),
                        total_documents=self._documents.count_for_kb(kb_id),
                    )
            except IntegrityError as exc:
                raise ConflictError("這個知識庫正在重建中") from exc

            # 重建會花錢也會改變所有人問到的答案——2A-4 的稽核清單該有這一條。
            # resource_id 由 middleware 從路徑取（`kb_id`），這裡補的是「切到哪去」。
            audit.describe(
                after={
                    "target_model": plan.target_model,
                    "target_embedding_version": plan.target_embedding_version,
                    "rechunk": plan.rechunk,
                }
            )
            view = self._view(job)

        # 交易提交之後才送任務（同 `enqueue_ingestion` 的理由：worker 可能在 COMMIT
        # 之前就開始處理，而它查不到這個 job）。送不出去不讓請求失敗——job 已經在
        # DB 裡，而 `advance` 的入口本來就存在。
        enqueue_reindex(tenant_id=tenant_id, job_id=uuid.UUID(str(job.id)))
        logger.info(
            "kb_reindex_started",
            kb_id=str(kb_id),
            job_id=str(view.id),
            target_model=plan.target_model,
            rechunk=plan.rechunk,
        )
        return view

    # ── 第 2、3 步：推進一輪 ────────────────────────────────────

    def advance(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> KbReindexJobView:
        """把 job 往前推一輪；回傳推完之後的狀態。

        **一輪不等於一批**：embedding 階段會把當下所有缺的向量算完（內部仍分 64 個
        一批各自落地），重切階段則只送 `RECHUNK_BATCH_SIZE` 份文件然後回來——後者
        要等 ETL，而等待期間佔著一個 worker 沒有意義。

        worker 反覆呼叫直到 terminal（`worker/reindex_tasks.py`）。
        """
        job = self._load(tenant_id, job_id)
        if job.status in {STATUS_COMPLETED, STATUS_FAILED}:
            return job

        if job.status == STATUS_PENDING:
            with tenant_context(tenant_id), unit_of_work():
                self._jobs.update(
                    job_id,
                    status=next_status(STATUS_PENDING, rechunk=job.rechunk),
                    started_at=timezone.now(),
                )
            job = self._load(tenant_id, job_id)

        try:
            if job.status == STATUS_RECHUNKING:
                return self._advance_rechunk(tenant_id, job)
            return self._advance_embedding(tenant_id, job)
        except Exception as exc:
            # 失敗要留下痕跡：不寫的話這個 job 會永遠停在進行中，而那個狀態同時
            # 擋住下一次重建（唯一約束的條件就是它）——使用者連重試的路都沒有。
            with tenant_context(tenant_id), unit_of_work():
                self._jobs.update(
                    job_id,
                    status=STATUS_FAILED,
                    error=error_payload(exc),
                    finished_at=timezone.now(),
                )
            logger.warning("kb_reindex_failed", job_id=str(job_id), exc_info=True)
            raise

    def latest(self, tenant_id: uuid.UUID, kb_id: uuid.UUID) -> KbReindexJobView | None:
        """這個 KB 最近一次重建（沒跑過回 None，由 API 轉 404）。"""
        with tenant_context(tenant_id), unit_of_work():
            self._require(kb_id)
            job = self._jobs.latest_for_kb(kb_id)
            return None if job is None else self._view(job)

    # ── 重切 ────────────────────────────────────────────────

    def _advance_rechunk(self, tenant_id: uuid.UUID, job: KbReindexJobView) -> KbReindexJobView:
        with tenant_context(tenant_id), unit_of_work():
            stored = self._jobs.get_by_id(job.id)
            cursor = None if stored is None else stored.rechunk_cursor
            batch = self._documents.for_kb_after(job.kb_id, after=cursor, limit=RECHUNK_BATCH_SIZE)

        processed = 0
        last_id: uuid.UUID | None = None
        for document in batch:
            try:
                self._documents_service.reingest(tenant_id, uuid.UUID(str(document.id)))
            except ConflictError:
                # 這一份正在被別的東西處理。**停在這裡而不是跳過**：跳過會讓游標
                # 越過它，而它會安靜地留著舊參數切出來的 chunk。下一輪再來。
                logger.info(
                    "kb_reindex_document_busy",
                    job_id=str(job.id),
                    document_id=str(document.id),
                )
                break
            processed += 1
            last_id = uuid.UUID(str(document.id))

        if processed:
            with tenant_context(tenant_id), unit_of_work():
                self._jobs.update(
                    job.id,
                    rechunk_cursor=last_id,
                    rechunked_documents=job.rechunked_documents + processed,
                )
            return self._load(tenant_id, job.id)

        # 這一輪一份都沒送出去：要嘛全部送完了，要嘛卡在一份處理中的文件。
        if batch:
            self._heartbeat(tenant_id, job.id)
            return job

        with tenant_context(tenant_id), unit_of_work():
            if self._documents.count_in_statuses_for_kb(job.kb_id, _STILL_PROCESSING):
                # 還在跑。**這裡不能往前推**：重切完 ≠ 重建完，新 chunk 這時大部分
                # 還沒有向量，而舊的已經 superseded 退出檢索。
                #
                # **但要留下心跳**：這段等待是合法的，而 `StuckReindexRescueService`
                # 認的就是 `updated_at`。不推的話，一個正在正常等 ETL 的大型重建會在
                # 門檻到期時被掃描器標成 failed。
                self._jobs.update(job.id)
                return job
            # 分母在**進入 embedding 階段時**才算得準——重切換掉了整批 chunk。
            self._jobs.update(
                job.id,
                status=STATUS_EMBEDDING,
                total_chunks=self._chunks.count_active_for_kb(kb_id=job.kb_id),
            )
        return self._advance_embedding(tenant_id, self._load(tenant_id, job.id))

    # ── 重嵌入 ──────────────────────────────────────────────

    def _advance_embedding(self, tenant_id: uuid.UUID, job: KbReindexJobView) -> KbReindexJobView:
        model = job.target_model
        embedded, tokens = self._embed_missing(tenant_id, job)

        if tokens > 0:
            # 只在真的算了東西時記（同 `EmbeddingService` 的理由：重跑不是使用者的
            # 行為，記 0 會灌水呼叫次數）。**整庫重建是一次可能數萬 chunk 的花費**，
            # 不入帳的話它在用量報表上完全看不見。
            self._usage.record(
                tenant_id,
                UsageEvent(
                    category="embedding",
                    model=model,
                    prompt_tokens=tokens,
                    request_id=f"reindex:{job.id}",
                ),
            )

        with tenant_context(tenant_id), unit_of_work():
            total = self._chunks.count_active_for_kb(kb_id=job.kb_id)
            done = self._embeddings.count_for_kb_version(
                kb_id=job.kb_id,
                model=model,
                embedding_version=job.target_embedding_version,
            )
            progress = ReindexProgress(total_chunks=total, embedded_chunks=min(done, total))
            self._jobs.update(job.id, total_chunks=total, embedded_chunks=progress.embedded_chunks)

            if ready_to_switch(status=STATUS_EMBEDDING, progress=progress):
                self._switch(job, model=model)

        logger.info(
            "kb_reindex_advanced",
            job_id=str(job.id),
            embedded=embedded,
            total_chunks=progress.total_chunks,
            # `prompt_tokens` 在 `config/logging.py` 的例外清單上（用量計數，非憑證）。
            prompt_tokens=tokens,
        )
        return self._load(tenant_id, job.id)

    def _embed_missing(self, tenant_id: uuid.UUID, job: KbReindexJobView) -> tuple[int, int]:
        """把還沒有目標版本向量的 chunk 算完；回傳 (筆數, token 數)。

        每一批各自落地（同 `EmbeddingService` 的第 2 條）：整個 KB 包在一個交易裡的
        話，provider 在後段不穩會把前面幾千筆的成功一起回滾，而下一輪又從頭開始。
        """
        with tenant_context(tenant_id), unit_of_work():
            active = self._chunks.for_retrieval(kb_id=job.kb_id)
            missing = set(
                self._embeddings.chunks_without_embedding(
                    [chunk.id for chunk in active],
                    model=job.target_model,
                    embedding_version=job.target_embedding_version,
                )
            )
        pending = [(chunk.id, chunk.content) for chunk in active if chunk.id in missing]

        embedded = 0
        tokens = 0
        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start : start + EMBED_BATCH_SIZE]
            result = self.gateway.embed([content for _, content in batch], model=job.target_model)
            rows: list[EmbeddingRow] = [
                {"chunk_id": chunk_id, "vector": vector}
                for (chunk_id, _), vector in zip(batch, result.vectors, strict=True)
            ]
            with tenant_context(tenant_id), unit_of_work():
                self._embeddings.upsert(
                    rows,
                    # provider 回報的名字而不是我們送的（06 §4）：唯一鍵要記的是真的
                    # 被用到的那一版。**切換時 KB 也會被設成這個名字**，否則檢索用
                    # 請求名去查，而向量存在回報名底下——一列都對不上。
                    model=result.model or job.target_model,
                    embedding_version=job.target_embedding_version,
                )
                if result.model and result.model != job.target_model:
                    self._jobs.update(job.id, target_model=result.model)
                    job = self._load(tenant_id, job.id)
            embedded += len(rows)
            tokens += result.usage.total_tokens

        return embedded, tokens

    # ── 第 3 步：原子切換 ──────────────────────────────────────

    def _switch(self, job: KbReindexJobView, *, model: str) -> None:
        """三個欄位在**同一個 UPDATE** 裡換掉（呼叫端已經在交易內）。

        分開寫的話，中間那一瞬間 KB 指向一個不存在的組合（新模型配舊版本號），
        而那一瞬間進來的查詢會拿到零筆——不是錯誤，是「知識庫突然沒有東西」。
        """
        self._knowledge_bases.update(
            job.kb_id,
            embedding_model=model,
            embedding_version=job.target_embedding_version,
            # 重切過才代表「現在的 chunk 是這一版設定切出來的」。純換模型的 job 帶的
            # 是原本的值（`plan_reindex`），寫回去是恆等操作。
            indexed_knowledge_version=job.target_knowledge_version,
        )
        now = timezone.now()
        self._jobs.update(
            job.id,
            status=STATUS_COMPLETED,
            # 第 4 步的保留窗從這裡起算，不是從 `created_at`。
            switched_at=now,
            finished_at=now,
        )
        logger.info(
            "kb_reindex_switched",
            job_id=str(job.id),
            kb_id=str(job.kb_id),
            model=model,
            embedding_version=job.target_embedding_version,
        )

    # ── 輔助 ────────────────────────────────────────────────

    def _heartbeat(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> None:
        """「我還活著」——把 `updated_at` 推回現在（`KbReindexJobRepository.update`）。

        重建有兩段是合法地什麼都不做：等一份處理中的文件讓開、等整批 ETL 跑完。
        那兩段沒有心跳的話，補償掃描會把正在正常跑的重建判成停滯。
        """
        with tenant_context(tenant_id), unit_of_work():
            self._jobs.update(job_id)

    def _require(self, kb_id: uuid.UUID) -> Any:
        """取 KB，不存在或屬於別的租戶都回同一個 404（09 §2.3 的資源類規則）。"""
        kb = self._knowledge_bases.get_by_id(kb_id)
        if kb is None:
            raise NotFoundError("知識庫不存在")
        return kb

    def _load(self, tenant_id: uuid.UUID, job_id: uuid.UUID) -> KbReindexJobView:
        with tenant_context(tenant_id), unit_of_work():
            job = self._jobs.get_by_id(job_id)
            if job is None:
                raise NotFoundError("重建工作不存在")
            return self._view(job)

    def _view(self, job: Any) -> KbReindexJobView:
        return KbReindexJobView(
            id=uuid.UUID(str(job.id)),
            kb_id=uuid.UUID(str(job.kb_id)),
            status=str(job.status),
            target_model=str(job.target_model),
            target_embedding_version=int(job.target_embedding_version),
            target_knowledge_version=int(job.target_knowledge_version),
            rechunk=bool(job.rechunk),
            total_chunks=int(job.total_chunks),
            embedded_chunks=int(job.embedded_chunks),
            total_documents=int(job.total_documents),
            rechunked_documents=int(job.rechunked_documents),
            started_at=job.started_at,
            switched_at=job.switched_at,
            finished_at=job.finished_at,
            error=job.error,
        )
