"""IngestionService —— ETL 的狀態機與編排（08 §2、§6）。

**這一層只做編排**：Extract / Clean / Chunk 的邏輯全在 `etl/`，讀寫全經 repository。
它負責的是那些「純函式不知道、但錯了會讓資料靜默壞掉」的事：

1. **狀態機推進**（uploaded → parsing → chunked；失敗 → failed）。文件的 status 是
   使用者看得到的進度，而 `etl_jobs` 是維運看的細節，兩者必須一致。
2. **冪等**（08 §6 的 `(doc_id, doc_version, stage)`）。Celery 是 at-least-once——
   重送不是意外，是規格。重跑不安全的話，症狀是同一段內容在檢索結果裡出現兩次。
3. **斷點續跑**。清洗完的 `ExtractedDoc` 落物件儲存（08 §2 的 `cleaned` 狀態），
   chunk 階段失敗重跑時不必重新解析一次 PDF。
4. **失敗分類**。毒檔與壞檔是**永久失敗**：重試三次只是把同一個錯誤做三遍，而且每次
   都吃一個 worker 的 CPU 與記憶體。基礎設施錯誤（物件儲存、DB 斷線）才重試——它
   往上拋，由 Celery 的退避重試處理（08 §6：30s/2m/10m）。

**回傳而不是拋出永久失敗**：呼叫端是 worker，它對「這份文件壞了」能做的只有記錄，
而那已經寫進 `document.error` 與 `etl_jobs`。拋出去只會讓 Celery 重試一件不會成功
的事。可重試的錯誤則相反——**必須**拋，否則任務會被當成處理完畢。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from config.logging import get_logger
from core.exceptions import (
    DomainError,
    ExtractionFailedError,
    NotFoundError,
    ObjectNotFoundError,
)
from core.object_storage import get_object, put_object
from core.tasks import enqueue_embedding
from core.tenant import tenant_context
from core.uow import unit_of_work
from etl.artifacts import from_json, to_json
from etl.chunk import ChunkConfig, TextChunk, chunk_document
from etl.clean import clean
from etl.extract.isolated import extract_isolated
from etl.extract.model import ExtractedDoc
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    EtlJobRepository,
    KnowledgeBaseRepository,
)
from services.knowledge.failures import error_payload

logger = get_logger(__name__)

STAGE_EXTRACT = "extract"
STAGE_CLEAN = "clean"
STAGE_CHUNK = "chunk"

STATUS_PARSING = "parsing"
STATUS_CLEANED = "cleaned"
STATUS_CHUNKED = "chunked"
STATUS_FAILED = "failed"

_JOB_SUCCEEDED = "succeeded"
_JOB_FAILED = "failed"

# **永久失敗**的例外型別：重試不會有不同結果。
#
# - `ExtractionFailedError`：毒檔、壞檔、逾時、不支援的型別。
# - `ObjectNotFoundError`：DB 說有、物件儲存說沒有。物件不會自己回來，重試三次只是
#   把同一個結論拖慢六分鐘，而 DLQ 裡還會標成 ``retryable=True``——那會誤導維運去
#   查一個不存在的環境問題。
_PERMANENT_FAILURES = (ExtractionFailedError, ObjectNotFoundError)


@dataclass(frozen=True)
class IngestionResult:
    document_id: uuid.UUID
    status: str
    chunk_count: int
    stats: dict[str, Any]


@dataclass(frozen=True)
class _Target:
    """跑這條 pipeline 需要知道的一切——**在交易內取一次快照**。

    每個階段各自開交易（它們之間有數十秒的 CPU 工作，握著交易不放會佔住連線並讓
    vacuum 停滯）。跨交易重讀 document 物件則會拿到別人改過的狀態，因此把不變的
    欄位固定在這裡。
    """

    document_id: uuid.UUID
    kb_id: uuid.UUID
    doc_version: int
    storage_key: str
    media_type: str
    chunk_config: ChunkConfig


class IngestionService:
    def __init__(
        self,
        *,
        documents: DocumentRepository | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        chunks: ChunkRepository | None = None,
        jobs: EtlJobRepository | None = None,
    ) -> None:
        self._documents = documents or DocumentRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._chunks = chunks or ChunkRepository()
        self._jobs = jobs or EtlJobRepository()

    def ingest(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> IngestionResult:
        """跑完 Extract → Clean → Chunk。

        文件不存在（或屬於別的租戶）時 raise `NotFoundError`：ETL 的租戶來自任務參數
        而非請求，參數錯了是程式錯誤，要在碰到任何資料之前停下來。
        """
        target = self._load_target(tenant_id, document_id)

        try:
            return self._run(tenant_id, target)
        except _PERMANENT_FAILURES as exc:
            # 重試不會有不同結果（見 `_PERMANENT_FAILURES`）——記錄後正常回傳，
            # 讓 Celery 不要把一個確定的結論做四遍。
            return self._fail_permanently(tenant_id, target, exc)

    def mark_retries_exhausted(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID, exc: Exception, *, attempts: int
    ) -> None:
        """重試耗盡 → 文件標 failed 並留下結構化原因（08 §6 的 DLQ 內容）。

        **與永久失敗刻意分成兩種紀錄**：``retryable=True`` 表示「這個錯誤本來是可以
        重試的，只是試完了」——那通常是基礎設施問題（物件儲存、DB、broker），處置是
        修環境後重跑；``retryable=False`` 是毒檔，重跑幾次都一樣。混成同一個狀態的話，
        維運面對一排 failed 文件時無從判斷該修什麼。

        Notification（通知使用者）屬 2A：這裡先把事實寫進 DB，那是通知的資料來源。
        """
        error = {
            "stage": self._last_running_stage(tenant_id, document_id),
            **error_payload(exc),
            "retryable": True,
            "attempts": attempts,
        }
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(document_id, status=STATUS_FAILED, error=error)
        logger.error(
            "ingestion_retries_exhausted",
            document_id=str(document_id),
            attempts=attempts,
            cause=error["cause"],
        )

    def _last_running_stage(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> str:
        """最後一個沒有成功的階段。

        重試耗盡時例外已經被 Celery 包過幾層，從它反推階段並不可靠；job 列才是
        事實來源（08 §6 要求 DLQ 內容含 stage）。
        """
        with tenant_context(tenant_id), unit_of_work():
            document = self._documents.get_by_id(document_id)
            if document is None:
                return STAGE_EXTRACT
            for stage in (STAGE_EXTRACT, STAGE_CLEAN, STAGE_CHUNK):
                job = self._jobs.find(
                    doc_id=document_id, doc_version=document.doc_version, stage=stage
                )
                if job is None or job.status != _JOB_SUCCEEDED:
                    return stage
        return STAGE_CHUNK

    # ── 編排 ────────────────────────────────────────────────

    def _run(self, tenant_id: uuid.UUID, target: _Target) -> IngestionResult:
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_PARSING, error=None)

        cleaned, clean_stats = self._materialise_cleaned(tenant_id, target)

        # 08 §2 的 ``cleaned``。**中間態不是裝飾**：一份 500 頁的 PDF 在 parsing 裡待
        # 好幾分鐘，而使用者與維運看到的是同一個字——分不出「還在解析」與「解析完了
        # 正在切塊」。兩者的處置不同（前者等，後者若卡住是 chunker 的問題）。
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_CLEANED, error=None)

        chunks = self._chunk_stage(tenant_id, target, cleaned)

        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_CHUNKED, error=None)

        # **交棒給 embedding 佇列**（06 §2 的 Q2），在 chunk 提交之後才送：worker 可能
        # 在 COMMIT 之前就開始處理，而它查到的是零個 chunk——那會把文件標成 ready，
        # 而它一個向量都沒有。少了這一行，整條鏈會安靜地停在 chunked：訊息沒有人送、
        # 佇列是空的、API 回應完全正常，只有文件永遠不會變 ready。
        enqueue_embedding(tenant_id=tenant_id, document_id=target.document_id)

        stats = {**clean_stats, "chunk_count": len(chunks)}
        logger.info(
            "ingestion_completed",
            document_id=str(target.document_id),
            chunk_count=len(chunks),
            language=clean_stats.get("language"),
        )
        return IngestionResult(
            document_id=target.document_id,
            status=STATUS_CHUNKED,
            chunk_count=len(chunks),
            stats=stats,
        )

    def _materialise_cleaned(
        self, tenant_id: uuid.UUID, target: _Target
    ) -> tuple[ExtractedDoc, dict[str, Any]]:
        """取得清洗後的文件：能續跑就從中間產物讀，否則重跑 extract + clean。"""
        resumed = self._resume_cleaned(tenant_id, target)
        if resumed is not None:
            return resumed

        raw = self._extract_stage(tenant_id, target)
        return self._clean_stage(tenant_id, target, raw)

    def _resume_cleaned(
        self, tenant_id: uuid.UUID, target: _Target
    ) -> tuple[ExtractedDoc, dict[str, Any]] | None:
        """clean 已成功過就從物件儲存讀回中間產物（08 §2 的斷點續跑）。

        讀不回來（物件被清掉、格式換版）**不是錯誤**：重跑 extract 就好，那只是多花
        時間。把它當錯誤的話，一次儲存端的清理會讓所有半成品文件永久卡住。
        """
        with tenant_context(tenant_id), unit_of_work():
            job = self._jobs.find(
                doc_id=target.document_id, doc_version=target.doc_version, stage=STAGE_CLEAN
            )
            stats = dict(job.stats) if job and job.status == _JOB_SUCCEEDED else None
        if stats is None:
            return None

        try:
            payload = get_object(self._artifact_key(target))
            return from_json(payload), stats
        except (DomainError, OSError):
            logger.info(
                "ingestion_artifact_unavailable_rerunning",
                document_id=str(target.document_id),
            )
            return None

    # ── 各階段 ──────────────────────────────────────────────

    def _extract_stage(self, tenant_id: uuid.UUID, target: _Target) -> ExtractedDoc:
        job_id = self._begin(tenant_id, target, STAGE_EXTRACT)
        try:
            content = get_object(target.storage_key)
            # 解析跑在子行程裡：畸形檔的 OOM 與無限迴圈不是 try/except 接得住的，
            # 而 worker 掛掉會停掉那台機器上所有租戶的 ETL（08 §6）。
            document = extract_isolated(content, media_type=target.media_type)
        except Exception as exc:
            self._fail_job(tenant_id, job_id, exc)
            raise
        self._finish(
            tenant_id,
            job_id,
            stats={
                "page_count": document.doc_meta.get("page_count"),
                "block_count": document.doc_meta.get("block_count"),
            },
        )
        return document

    def _clean_stage(
        self, tenant_id: uuid.UUID, target: _Target, document: ExtractedDoc
    ) -> tuple[ExtractedDoc, dict[str, Any]]:
        job_id = self._begin(tenant_id, target, STAGE_CLEAN)
        try:
            cleaned, clean_stats = clean(document)
            stats: dict[str, Any] = {
                "total_blocks": clean_stats.total_blocks,
                "dropped_blocks": clean_stats.dropped_blocks,
                "removed_headers": clean_stats.removed_headers,
                "drop_rate": round(clean_stats.drop_rate, 4),
                # 08 §4：丟棄率 > 20% 示警——它是「這份來源要不要人看一眼」的訊號。
                "quality_warning": clean_stats.quality_warning,
                "language": clean_stats.language,
            }
            # 中間產物先落地再標成功：反過來的話，下一次重跑會相信一個不存在的產物，
            # 而 `_resume_cleaned` 的讀取失敗雖然接得住，那條路徑不該是常態。
            put_object(
                self._artifact_key(target),
                to_json(cleaned).encode("utf-8"),
                content_type="application/json",
            )
        except Exception as exc:
            self._fail_job(tenant_id, job_id, exc)
            raise
        self._finish(tenant_id, job_id, stats=stats)
        return cleaned, stats

    def _chunk_stage(
        self, tenant_id: uuid.UUID, target: _Target, cleaned: ExtractedDoc
    ) -> list[TextChunk]:
        job_id = self._begin(tenant_id, target, STAGE_CHUNK)
        try:
            chunks = chunk_document(cleaned, config=target.chunk_config)
            with tenant_context(tenant_id), unit_of_work():
                self._chunks.replace_for_version(
                    document_id=target.document_id,
                    kb_id=target.kb_id,
                    doc_version=target.doc_version,
                    rows=[self._chunk_row(chunk) for chunk in chunks],
                )
        except Exception as exc:
            self._fail_job(tenant_id, job_id, exc)
            raise
        total_tokens = sum(chunk.token_count for chunk in chunks)
        self._finish(
            tenant_id,
            job_id,
            stats={
                "chunk_count": len(chunks),
                "avg_tokens": round(total_tokens / len(chunks), 1) if chunks else 0,
            },
        )
        return chunks

    # ── 輔助 ────────────────────────────────────────────────

    def _load_target(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> _Target:
        with tenant_context(tenant_id), unit_of_work():
            document = self._documents.get_by_id(document_id)
            if document is None:
                raise NotFoundError("文件不存在")
            kb = self._knowledge_bases.get_by_id(document.kb_id)
            if kb is None:
                raise NotFoundError("知識庫不存在")
            return _Target(
                document_id=document.id,
                kb_id=document.kb_id,
                doc_version=document.doc_version,
                storage_key=document.storage_key,
                media_type=document.mime_type,
                chunk_config=_chunk_config_from(kb.config),
            )

    def _artifact_key(self, target: _Target) -> str:
        """中間產物的 key。**帶 doc_version**：re-ingest 產生的是另一份內容，
        共用 key 的話新版本會讀到舊版的清洗結果，而兩者的差別正是重跑的理由。"""
        return f"{target.storage_key}/v{target.doc_version}/cleaned.json"

    def _chunk_row(self, chunk: TextChunk) -> dict[str, Any]:
        return {
            "seq": chunk.seq,
            "content": chunk.text,
            "token_count": chunk.token_count,
            # meta 是 1D 的引用要用的東西：頁碼與所屬章節（05 §3.2）。
            "meta": {"page": chunk.page, "heading_path": list(chunk.heading_path)},
        }

    def _begin(self, tenant_id: uuid.UUID, target: _Target, stage: str) -> uuid.UUID:
        with tenant_context(tenant_id), unit_of_work():
            job = self._jobs.start(
                doc_id=target.document_id, doc_version=target.doc_version, stage=stage
            )
            return uuid.UUID(str(job.id))

    def _finish(self, tenant_id: uuid.UUID, job_id: uuid.UUID, *, stats: dict[str, Any]) -> None:
        with tenant_context(tenant_id), unit_of_work():
            self._jobs.finish(job_id, status=_JOB_SUCCEEDED, stats=stats)

    def _fail_job(self, tenant_id: uuid.UUID, job_id: uuid.UUID, exc: Exception) -> None:
        with tenant_context(tenant_id), unit_of_work():
            self._jobs.finish(job_id, status=_JOB_FAILED, error=error_payload(exc))

    def _fail_permanently(
        self, tenant_id: uuid.UUID, target: _Target, exc: DomainError
    ) -> IngestionResult:
        stage = self._failing_stage(tenant_id, target)
        error = {"stage": stage, **error_payload(exc), "retryable": False}
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_FAILED, error=error)
        logger.warning(
            "ingestion_failed",
            document_id=str(target.document_id),
            stage=stage,
            cause=error.get("cause"),
        )
        return IngestionResult(
            document_id=target.document_id,
            status=STATUS_FAILED,
            chunk_count=0,
            stats={"error": error},
        )

    def _failing_stage(self, tenant_id: uuid.UUID, target: _Target) -> str:
        """哪一個階段掛的——問 job 列，不從例外反推。

        例外只知道「解析失敗」，而使用者與 DLQ 要知道的是「卡在哪一步」（08 §6）。
        """
        with tenant_context(tenant_id), unit_of_work():
            for stage in (STAGE_EXTRACT, STAGE_CLEAN, STAGE_CHUNK):
                job = self._jobs.find(
                    doc_id=target.document_id, doc_version=target.doc_version, stage=stage
                )
                if job is not None and job.status == _JOB_FAILED:
                    return str(stage)
        return STAGE_EXTRACT


def _chunk_config_from(config: dict[str, Any]) -> ChunkConfig:
    """KB config → `ChunkConfig`（06 §2.1：策略與參數由 KB 決定）。

    只認得自己支援的鍵，其餘忽略：KB config 是使用者可寫的 JSON，把未知的鍵直接
    展開成建構參數會讓一個打錯的欄位變成 TypeError，而那發生在 worker 裡。
    """
    raw = config.get("chunk")
    section: dict[str, Any] = raw if isinstance(raw, dict) else {}
    defaults = ChunkConfig()
    return ChunkConfig(
        target_tokens=int(section.get("target_tokens", defaults.target_tokens)),
        overlap_tokens=int(section.get("overlap_tokens", defaults.overlap_tokens)),
    )
