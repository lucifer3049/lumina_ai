"""EmbeddingService —— 寫路徑的最後一段（06 §2、08 §2 的 ``chunked → embedding → ready``）。

`IngestionService` 把文件切成 chunk，這裡把 chunk 變成向量。兩者的形狀刻意一致
（同一組冪等鍵、同一種失敗分類、同一份錯誤 payload），但有一個決定性的差別：

**這裡的重跑要花錢。** ETL 重跑只是再燒一次 CPU；embedding 重跑是再打一次 provider
的 API。因此「哪些 chunk 還沒有向量」不是最佳化，是這一層的核心邏輯——
`EmbeddingRepository.chunks_without_embedding` 是唯一的依據，而它走
``(tenant_id, chunk_id)`` 索引（05 §4）。

四條規則：

1. **批次上限 64**（06 §2.1）。不分批的話，一份 500 頁的 PDF 會變成一次帶上千段文字
   的請求，provider 以 413 或 token 上限退回整批——症狀是「大文件永遠處理不完，小
   文件都正常」。
2. **每批各自落地**。整份文件包在一個交易裡的話，provider 在後段不穩會把前段的成功
   一起回滾，於是每次重跑都從第 1 批開始，而它每次都會在某處再炸一次。
3. **失敗分兩類**（08 §6）。配額用盡與模型未啟用重試幾次都一樣 → 永久失敗，記進
   `document.error` 後正常回傳；429、5xx、逾時 → 往上拋，讓 Celery 退避重試。
4. **模型與版本來自 KB**（06 §2.2）。重嵌入的做法是「新版本算完 → 原子切換 → 清理
   舊版」，那要求同一個 chunk 能同時有兩個版本的向量。寫死全域值的話，切換就只剩
   「先刪再寫」，而那幾分鐘檢索什麼都查不到。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ai.gateway import AIGateway, build_gateway
from config.logging import get_logger
from core.exceptions import NotFoundError, ProviderError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.knowledge import (
    ChunkRepository,
    DocumentRepository,
    EmbeddingRepository,
    EmbeddingRow,
    EtlJobRepository,
    KnowledgeBaseRepository,
)
from services.knowledge.failures import error_payload

logger = get_logger(__name__)

STAGE_EMBED = "embed"

STATUS_CHUNKED = "chunked"
STATUS_EMBEDDING = "embedding"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# 只有這幾個狀態該進 embedding（08 §2：``chunked → embedding → ready``）。
#
# **這是一道防呆，不是樂觀鎖**。訊息可能比它描述的世界舊：文件在 chunked 時排了
# embedding，使用者接著按 re-ingest（那時 chunked 是允許重跑的），於是 doc_version
# 變成 2 而新版的 chunk 還沒切出來。那個舊訊息這時進來會看到「這一版沒有 chunk 要
# 算」，然後把文件標成 ready——一份零向量、狀態卻是完成的文件。ETL 稍後會把它蓋回
# 去，所以它自己會好，但那段期間檢索查得到它、而它什麼都答不出來。
_EMBEDDABLE_STATUSES = frozenset({STATUS_CHUNKED, STATUS_EMBEDDING, STATUS_READY})

_JOB_SUCCEEDED = "succeeded"
_JOB_FAILED = "failed"

# 06 §2.1：batch=64。**不是隨手取的整數**——它同時受兩個上限夾住：provider 的單次
# 請求 token 上限，以及「一批失敗要重算多少」。調大省的是往返次數（次要），賠的是
# 失敗時的重算量（真的錢）。
EMBED_BATCH_SIZE = 64


@dataclass(frozen=True)
class EmbeddingResult:
    document_id: uuid.UUID
    status: str
    embedded_count: int
    stats: dict[str, Any]


@dataclass(frozen=True)
class _Target:
    """跑這一段需要知道的一切——**在交易內取一次快照**（理由同 IngestionService）。"""

    document_id: uuid.UUID
    doc_version: int
    status: str
    model: str
    embedding_version: int


class EmbeddingService:
    def __init__(
        self,
        *,
        gateway: AIGateway | None = None,
        documents: DocumentRepository | None = None,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        chunks: ChunkRepository | None = None,
        embeddings: EmbeddingRepository | None = None,
        jobs: EtlJobRepository | None = None,
    ) -> None:
        # Gateway 惰性建立：`build_gateway()` 會讀設定並解析 provider 名稱，而未實作的
        # provider 會直接 raise。建構 service 本身不該因此失敗——mark_retries_exhausted
        # 這條路徑根本不打 provider。
        self._gateway = gateway
        self._documents = documents or DocumentRepository()
        self._knowledge_bases = knowledge_bases or KnowledgeBaseRepository()
        self._chunks = chunks or ChunkRepository()
        self._embeddings = embeddings or EmbeddingRepository()
        self._jobs = jobs or EtlJobRepository()

    @property
    def gateway(self) -> AIGateway:
        if self._gateway is None:
            self._gateway = build_gateway()
        return self._gateway

    def embed_document(self, tenant_id: uuid.UUID, document_id: uuid.UUID) -> EmbeddingResult:
        """把一份文件的 active chunk 全部轉成向量，成功後推進到 ``ready``。

        文件不存在（或屬於別的租戶）時 raise `NotFoundError`：租戶來自任務參數而非
        請求，參數錯了是程式錯誤，要在碰到任何資料之前停下來。安靜地回「沒有 chunk
        要算」更糟——那會把文件標成 ready，而它一個向量都沒有。
        """
        target = self._load_target(tenant_id, document_id)
        if target.status not in _EMBEDDABLE_STATUSES:
            # 訊息比世界舊（見 `_EMBEDDABLE_STATUSES`）。**什麼都不碰**就回傳：ETL
            # 正在重跑這份文件，它跑完會自己再排一次 embedding。
            logger.info(
                "embedding_skipped_stale_task",
                document_id=str(document_id),
                document_status=target.status,
            )
            return EmbeddingResult(
                document_id=target.document_id,
                status=target.status,
                embedded_count=0,
                stats={"skipped": "document_not_chunked"},
            )

        job_id = self._begin(tenant_id, target)

        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_EMBEDDING, error=None)

        try:
            embedded, tokens, batches = self._embed_pending(tenant_id, target)
        except ProviderError as exc:
            if exc.retryable:
                # 429／5xx／逾時：稍後再送就會成功。往上拋讓 Celery 走 08 §6 的退避。
                # 在這裡吞掉的話，任務會被當成處理完畢，而文件停在 embedding 且沒有
                # 人再碰它。**這時不標 failed**——使用者會看到一個十分鐘後自己好掉的
                # 錯誤，而那比沒有訊息更難解釋。
                self._fail_job(tenant_id, job_id, exc)
                raise
            return self._fail_permanently(tenant_id, target, job_id, exc)
        except Exception as exc:
            self._fail_job(tenant_id, job_id, exc)
            raise

        stats = {"embedded_count": embedded, "prompt_tokens": tokens, "batches": batches}
        self._finish(tenant_id, job_id, stats=stats)

        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_READY, error=None)

        logger.info(
            "embedding_completed",
            document_id=str(target.document_id),
            embedded_count=embedded,
            model=target.model,
            batches=batches,
            # `prompt_tokens` 在 `config/logging.py` 的例外清單上——它是用量計數，
            # 不是憑證。少了那筆例外，這個欄位會印成 ``***``（見該檔的清單註解）。
            prompt_tokens=tokens,
        )
        return EmbeddingResult(
            document_id=target.document_id,
            status=STATUS_READY,
            embedded_count=embedded,
            stats=stats,
        )

    def mark_retries_exhausted(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID, exc: Exception, *, attempts: int
    ) -> None:
        """重試耗盡 → 文件標 failed 並留下結構化原因（08 §6 的 DLQ 內容）。

        ``retryable=True`` 與永久失敗的 ``False`` 分得開：前者是 provider 或網路的
        問題，處置是修環境後重跑；後者是設定或配額，重跑幾次都一樣。混成同一個狀態
        時，維運面對一排 failed 文件無從判斷該修什麼。
        """
        error = {
            "stage": STAGE_EMBED,
            **error_payload(exc),
            "retryable": True,
            "attempts": attempts,
        }
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(document_id, status=STATUS_FAILED, error=error)
        logger.error(
            "embedding_retries_exhausted",
            document_id=str(document_id),
            attempts=attempts,
            cause=error["cause"],
        )

    # ── 編排 ────────────────────────────────────────────────

    def _embed_pending(self, tenant_id: uuid.UUID, target: _Target) -> tuple[int, int, int]:
        """算完所有還沒有向量的 chunk；回傳 (筆數, token 數, 批次數)。

        每一批各自寫入（見模組 docstring 的第 2 條）：中途失敗時前面幾批留在 DB，
        重跑只補剩下的。
        """
        pending = self._pending_chunks(tenant_id, target)
        if not pending:
            # 空批次不打 provider（Gateway 也會擋，這裡先擋是為了連 job 都不必轉一圈）。
            # 零 chunk 的文件（空檔、只有圖的 PDF）是**成功**，不是失敗——標成 failed
            # 的話使用者會看到一個沒有東西可修的錯誤。
            return 0, 0, 0

        embedded = 0
        tokens = 0
        batches = 0
        for start in range(0, len(pending), EMBED_BATCH_SIZE):
            batch = pending[start : start + EMBED_BATCH_SIZE]
            result = self.gateway.embed([content for _, content in batch], model=target.model)
            rows: list[EmbeddingRow] = [
                {"chunk_id": chunk_id, "vector": vector}
                for (chunk_id, _), vector in zip(batch, result.vectors, strict=True)
            ]
            with tenant_context(tenant_id), unit_of_work():
                self._embeddings.upsert(
                    rows,
                    # provider 回報的 model 而不是我們送的（06 §4）：別名解析
                    # （``text-embedding-3-small`` → 帶日期的實際版本）之後，唯一鍵
                    # 要記的是真的被用到的那一個。
                    model=result.model,
                    embedding_version=target.embedding_version,
                )
            embedded += len(rows)
            tokens += result.usage.total_tokens
            batches += 1

        return embedded, tokens, batches

    def _pending_chunks(self, tenant_id: uuid.UUID, target: _Target) -> list[tuple[uuid.UUID, str]]:
        """還沒有向量的 chunk（id 與內容），**只含目前版本且未 superseded 的**。

        舊版 chunk 即將被清理 job 硬刪（2A），在那之前算一次是純粹的浪費：一份重跑
        三次的大文件會付四份錢，其中三份的資料幾分鐘後就被刪掉。
        """
        with tenant_context(tenant_id), unit_of_work():
            active = self._chunks.active_for_version(
                document_id=target.document_id, doc_version=target.doc_version
            )
            missing = set(
                self._embeddings.chunks_without_embedding(
                    [chunk.id for chunk in active],
                    model=target.model,
                    embedding_version=target.embedding_version,
                )
            )
        return [(chunk.id, chunk.content) for chunk in active if chunk.id in missing]

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
                doc_version=document.doc_version,
                status=str(document.status),
                model=model_for(kb.embedding_model),
                embedding_version=int(kb.embedding_version),
            )

    def _begin(self, tenant_id: uuid.UUID, target: _Target) -> uuid.UUID:
        with tenant_context(tenant_id), unit_of_work():
            job = self._jobs.start(
                doc_id=target.document_id, doc_version=target.doc_version, stage=STAGE_EMBED
            )
            return uuid.UUID(str(job.id))

    def _finish(self, tenant_id: uuid.UUID, job_id: uuid.UUID, *, stats: dict[str, Any]) -> None:
        with tenant_context(tenant_id), unit_of_work():
            self._jobs.finish(job_id, status=_JOB_SUCCEEDED, stats=stats)

    def _fail_job(self, tenant_id: uuid.UUID, job_id: uuid.UUID, exc: Exception) -> None:
        with tenant_context(tenant_id), unit_of_work():
            self._jobs.finish(job_id, status=_JOB_FAILED, error=error_payload(exc))

    def _fail_permanently(
        self, tenant_id: uuid.UUID, target: _Target, job_id: uuid.UUID, exc: ProviderError
    ) -> EmbeddingResult:
        """重試不會有不同結果（配額用盡、模型未啟用）——記錄後正常回傳。

        拋出去只會讓 Celery 把一個確定的結論做四遍，而每一遍都要等完整的退避。
        """
        error = {"stage": STAGE_EMBED, **error_payload(exc), "retryable": False}
        self._fail_job(tenant_id, job_id, exc)
        with tenant_context(tenant_id), unit_of_work():
            self._documents.set_status(target.document_id, status=STATUS_FAILED, error=error)
        logger.warning(
            "embedding_failed",
            document_id=str(target.document_id),
            model=target.model,
            cause=error.get("cause"),
        )
        return EmbeddingResult(
            document_id=target.document_id,
            status=STATUS_FAILED,
            embedded_count=0,
            stats={"error": error},
        )


def model_for(kb_embedding_model: str) -> str:
    """KB 沒指定模型時退回設定的預設值（鐵則 9：不 hardcode 模型名）。

    **不能讓空字串落地**：它會照樣寫進 ``UNIQUE(chunk_id, model, embedding_version)``，
    於是那批向量永遠對不上任何一次檢索（檢索用的是設定值），而資料看起來完全正常
    ——症狀是「這份文件明明 ready，卻從來不會被查到」。
    """
    if kb_embedding_model:
        return kb_embedding_model

    from config.settings.app_settings import get_app_settings

    return str(get_app_settings().ai_embedding_model)
