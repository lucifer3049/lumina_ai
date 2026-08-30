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
from common.document_status import EMBEDDABLE_STATUSES, DocumentStatus
from config.logging import get_logger
from core.exceptions import NotFoundError, ProviderError
from core.redis import get_redis, tenant_key
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
from services.platform.notifications import NotificationService
from services.platform.usage import UsageEvent, UsageService

logger = get_logger(__name__)

STAGE_EMBED = "embed"

# 狀態值與「哪些狀態可進 embedding」都出自 `common.document_status`（2026-08-30
# 收斂）：入口的 `EMBEDDABLE_STATUSES` 判斷是防呆——訊息可能比它描述的世界舊
# （文件在 chunked 時排了 embedding，使用者接著按 re-ingest，doc_version +1 而新版
# chunk 還沒切出來）。防呆擋不住「檢查之後世界才前進」，那一半由**寫入端**的
# `set_status(expected_status=…, expected_doc_version=…)` 守——舊任務的收尾寫入
# 會整筆落空，而不是把一份零 chunk 的文件標成 ready。

_JOB_SUCCEEDED = "succeeded"
_JOB_FAILED = "failed"

# provider 把請求的模型名解析成什麼（見 `EmbeddingService._resolved_model`）。**這是
# 快取，不是設定**：不知道就退回請求名，最多多問一輪；記錯了下一次呼叫會自己蓋掉。
# TTL 一天——別名的指向會隨 provider 改版而變，而過期的代價只有一批的重算。
_MODEL_ALIAS_KEY = "embed-model"
_MODEL_ALIAS_TTL_SECONDS = 86_400

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
        usage: UsageService | None = None,
        notifications: NotificationService | None = None,
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
        self._usage = usage or UsageService()
        self._notifications = notifications or NotificationService()

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
        if target.status not in EMBEDDABLE_STATUSES:
            # 訊息比世界舊（見模組頂部的守門說明）。**什麼都不碰**就回傳：ETL
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
            # 帶 expected_*：入口檢查與這一行之間 re-ingest 可能已經發生（版本 +1、
            # 狀態重設）。落空（0 列）代表世界前進了——這個任務手上的一切都是舊的。
            moved = self._documents.set_status(
                target.document_id,
                status=DocumentStatus.EMBEDDING,
                error=None,
                expected_status=EMBEDDABLE_STATUSES,
                expected_doc_version=target.doc_version,
            )
        if not moved:
            return self._skip_stale(target, at="enter_embedding")

        try:
            embedded, tokens, batches, reported_model = self._embed_pending(tenant_id, target)
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

        # usage_logs 落地（2A-1）。**只在有消費時記**：冪等重跑（task 重試、手動重推）
        # 沒送任何東西去算就沒有列——記 0 會灌水呼叫次數，而重試不是使用者的行為。
        # record 不往外拋（旁路原則）；已知縮水：中途失敗的批次其 tokens 不會入帳
        # （成功批次的向量留在 DB、重跑只補缺），2A-2 對帳時評估要不要逐批記。
        if tokens > 0:
            self._usage.record(
                tenant_id,
                UsageEvent(
                    category="embedding",
                    model=reported_model,
                    prompt_tokens=tokens,
                    request_id=f"embed:{target.document_id}:v{target.doc_version}",
                ),
            )

        with tenant_context(tenant_id), unit_of_work():
            # **這一行是「零 chunk 卻 ready」競態的關門處**（2026-08-30 深度審查）：
            # 生成向量期間文件被 re-ingest 的話，這裡的 doc_version 對不上、寫入落空
            # ——舊任務不得把新版本（chunks 還沒切出來）標成 ready。已算好的向量留在
            # DB 不回滾：新版本的 embedding 會自己跳過已存在的（冪等鍵），沒有浪費。
            promoted = self._documents.set_status(
                target.document_id,
                status=DocumentStatus.READY,
                error=None,
                expected_status=(DocumentStatus.EMBEDDING,),
                expected_doc_version=target.doc_version,
            )
        if not promoted:
            return self._skip_stale(target, at="promote_ready")

        # 08 §2 的終點——「可以問了」的那一刻，也是唯一值得通知使用者的一刻
        # （chunked 對他沒有意義）。同一批上傳會被收合成一則（2A-5）。
        self._notifications.notify_document_ready(tenant_id, target.document_id)

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
            status=DocumentStatus.READY,
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
            # 只蓋還停在 chunked／embedding 的：重試等待期間文件可能已被 re-ingest
            # （狀態重設回 uploaded、版本 +1），那時這個結論屬於舊版本——標上去會把
            # 一份正在重跑的文件蓋成 failed。手上沒有版本快照（本路徑只有 id），
            # 以狀態作守門；ready 也不蓋（重送舊訊息不得把完成的文件改成失敗）。
            marked = self._documents.set_status(
                document_id,
                status=DocumentStatus.FAILED,
                error=error,
                expected_status=(DocumentStatus.CHUNKED, DocumentStatus.EMBEDDING),
            )
        if not marked:
            logger.info("embedding_exhausted_but_stale", document_id=str(document_id))
            return
        logger.error(
            "embedding_retries_exhausted",
            document_id=str(document_id),
            attempts=attempts,
            cause=error["cause"],
        )
        self._notifications.notify_document_failed(tenant_id, document_id, error=error)

    # ── 編排 ────────────────────────────────────────────────

    def _embed_pending(self, tenant_id: uuid.UUID, target: _Target) -> tuple[int, int, int, str]:
        """算完所有還沒有向量的 chunk；回傳 (筆數, token 數, 批次數, provider 回報的 model)。

        每一批各自寫入（見模組 docstring 的第 2 條）：中途失敗時前面幾批留在 DB，
        重跑只補剩下的。
        """
        # **問「哪些算過了」要用寫入時記的那個名字**，而寫入記的是 provider 回報值
        # （見下面 upsert 的註解）。兩者在會做別名解析的 provider 上不同，見
        # `_resolved_model` 的 docstring。
        lookup_model = self._resolved_model(tenant_id, target.model)
        pending = self._pending_chunks(tenant_id, target, model=lookup_model)
        if not pending:
            # 空批次不打 provider（Gateway 也會擋，這裡先擋是為了連 job 都不必轉一圈）。
            # 零 chunk 的文件（空檔、只有圖的 PDF）是**成功**，不是失敗——標成 failed
            # 的話使用者會看到一個沒有東西可修的錯誤。
            return 0, 0, 0, target.model

        embedded = 0
        tokens = 0
        batches = 0
        reported_model = target.model
        start = 0
        while start < len(pending):
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
            start += len(batch)
            # 計價按實際被用到的那一個（同 upsert 記 result.model 的理由）。
            reported_model = result.model or reported_model

            if result.model and result.model != lookup_model:
                # **剛才那一輪 pending 是拿錯名字問出來的**：已經有向量的 chunk 全部
                # 落在回報名底下，而我們用請求名去找，於是它們每一個都被當成「還沒
                # 算」。記住這次的解析結果，然後重問一次——剛寫進去的那批自然不在新
                # 清單裡，早先幾輪算過的也不在。**這個分支每次執行最多走一次**，之後
                # 兩個名字就一致了。
                self._remember_resolution(tenant_id, target.model, result.model)
                lookup_model = result.model
                pending = self._pending_chunks(tenant_id, target, model=lookup_model)
                start = 0

        return embedded, tokens, batches, reported_model

    def _pending_chunks(
        self, tenant_id: uuid.UUID, target: _Target, *, model: str
    ) -> list[tuple[uuid.UUID, str]]:
        """還沒有向量的 chunk（id 與內容），**只含目前版本且未 superseded 的**。

        舊版 chunk 即將被清理 job 硬刪（2A），在那之前算一次是純粹的浪費：一份重跑
        三次的大文件會付四份錢，其中三份的資料幾分鐘後就被刪掉。

        ``model`` 由呼叫端決定而不是取 ``target.model``：這個查詢要對得上**寫入時
        用的名字**，而那是 provider 回報值（見 `_resolved_model`）。
        """
        with tenant_context(tenant_id), unit_of_work():
            active = self._chunks.active_for_version(
                document_id=target.document_id, doc_version=target.doc_version
            )
            missing = set(
                self._embeddings.chunks_without_embedding(
                    [chunk.id for chunk in active],
                    model=model,
                    embedding_version=target.embedding_version,
                )
            )
        return [(chunk.id, chunk.content) for chunk in active if chunk.id in missing]

    # ── 別名解析的記憶（見 `_embed_pending`）──────────────────

    def _resolved_model(self, tenant_id: uuid.UUID, requested: str) -> str:
        """請求的模型名 → 上次 provider 回報的那個名字（不知道就用請求名）。

        **為什麼需要這一層。** 寫入端記的是回報名（別名解析之後真的被用到的版本，
        06 §4），檢索端查的也是回報名（`RetrievalService._search`——它每次都先嵌入
        查詢，所以手上一定有回報名）。只有「哪些 chunk 還沒有向量」這個查詢手上沒有
        ——它跑在第一次呼叫 provider **之前**。

        拿請求名去問的後果不是查錯一點點，而是**永遠回「全部都沒算過」**：向量存在
        回報名底下，請求名底下一列都沒有。於是 task 重試、rescue 補送、每一次重跑都
        把整份文件重新算一遍——而這一層 docstring 自稱「哪些算過了是核心邏輯」。
        Mock provider 回報同名，所以測試看不到這件事。

        存 Redis 而不是欄位：這是「provider 目前把這個別名解析成什麼」的快取，不是
        使用者的設定（KB 的 `embedding_model` 是後者，蓋掉它等於偷改使用者的設定）。
        解析結果會隨 provider 改版而變，所以給 TTL——過期的代價只是一輪重問。
        """
        cached = get_redis().get(tenant_key(tenant_id, _MODEL_ALIAS_KEY, requested))
        return str(cached) if cached else requested

    def _remember_resolution(self, tenant_id: uuid.UUID, requested: str, reported: str) -> None:
        get_redis().set(
            tenant_key(tenant_id, _MODEL_ALIAS_KEY, requested),
            reported,
            ex=_MODEL_ALIAS_TTL_SECONDS,
        )

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
            # 守門同 ready 那一行：re-ingest 之後這個結論屬於舊版本，不蓋新世界。
            marked = self._documents.set_status(
                target.document_id,
                status=DocumentStatus.FAILED,
                error=error,
                expected_doc_version=target.doc_version,
            )
        if not marked:
            return self._skip_stale(target, at="mark_failed")
        logger.warning(
            "embedding_failed",
            document_id=str(target.document_id),
            model=target.model,
            cause=error.get("cause"),
        )
        self._notifications.notify_document_failed(tenant_id, target.document_id, error=error)
        return EmbeddingResult(
            document_id=target.document_id,
            status=DocumentStatus.FAILED,
            embedded_count=0,
            stats={"error": error},
        )

    def _skip_stale(self, target: _Target, *, at: str) -> EmbeddingResult:
        """收尾寫入落空（expected_* 對不上）＝世界已經前進：什麼都不碰、原樣退場。

        新版本的 ETL 鏈會自己再排一次 embedding；這裡多做任何一步（通知、標狀態）
        都是替一個已經不存在的版本發言。
        """
        logger.info(
            "embedding_superseded_mid_flight",
            document_id=str(target.document_id),
            doc_version=target.doc_version,
            at=at,
        )
        return EmbeddingResult(
            document_id=target.document_id,
            status=str(target.status),
            embedded_count=0,
            stats={"skipped": "superseded_mid_flight", "at": at},
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
