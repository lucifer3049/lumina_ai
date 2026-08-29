"""Knowledge 的 Repository —— 租戶隔離的第一道防線（鐵則 4）。

第二道是 RLS policy（apps/knowledge/migrations/0002_rls.py）。兩道的條件必須一致；
不一致時的症狀分兩種，而且都沒有錯誤訊息（程式比 policy 寬 → 回空集合；程式比
policy 窄 → 使用者看不到本來該看到的資料）。

本檔的方法全部是**同步**的：只會被 :func:`core.db.run_orm` 從 threadpool 呼叫，
不得在 async 路徑直接 await（ADR-001）。

**三個查詢語意在這裡定案，錯了都不會報錯**：

1. :meth:`ChunkRepository.for_retrieval` 預設排除 ``superseded``。混進舊版本的話，
   LLM 會拿早已被取代的內容當依據，而引用指向的 chunk 確實存在——回應看起來完全
   正常，事後沒有任何自動化手段能發現。
2. :meth:`EtlJobRepository.find` 以 ``(document, doc_version, stage)`` 定位（08 §6）。
   少了 ``doc_version``，re-ingest 會查到上一版已成功的 job 而跳過該階段，文件停在
   舊內容、狀態卻是 ready。
3. **軟刪除的實體預設不可見**（1B-2）：`KnowledgeBase` 與 `Document` 的
   ``get_queryset`` 排除 ``deleted_at IS NOT NULL``。05 §5.4 的軟刪除是為了「使用者
   可能後悔」（保留窗過後由 `DeletedKnowledgePurgeService` 硬刪，窗長見
   ``retention_purge_after_days``），不是為了讓資料繼續出現——只寫 ``deleted_at``
   卻沒從查詢排除的話，使用者會看到自己剛刪掉的東西還在列表上，而刪除 API 回了 204。

   **但這條規則只蓋得住「這一張表的查詢」。** 檢索讀的是 chunk 與 embedding，它們
   不繼承這個 queryset——文件刪除因此要另外把 chunk 標成 ``superseded``（見
   `DocumentService.delete`），否則已刪文件的內容會繼續出現在回答的引用裡。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from django.db import connection, models
from django.utils import timezone
from pgvector import HalfVector
from pgvector.django import CosineDistance

from apps.knowledge.models import (
    Chunk,
    Document,
    Embedding,
    EtlJob,
    KbReindexJob,
    KnowledgeBase,
)
from core.tenant import get_current_tenant_id
from repositories.base import SoftDeletableRepository, TenantScopedRepository


class KnowledgeBaseRepository(SoftDeletableRepository[KnowledgeBase]):
    model = KnowledgeBase

    def get_by_id(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return self.get_queryset().filter(id=kb_id).first()

    def list_all(self) -> list[KnowledgeBase]:
        return list(self.get_queryset().order_by("-created_at"))

    def create(
        self, *, name: str, description: str = "", config: dict[str, object] | None = None
    ) -> KnowledgeBase:
        return KnowledgeBase.objects.create(
            tenant_id=get_current_tenant_id(operation="KnowledgeBaseRepository.create"),
            name=name,
            description=description,
            config=config or {},
        )

    def update(self, kb_id: uuid.UUID, **fields: object) -> int:
        """部分更新——只寫呼叫端明確給的欄位。

        ``**fields`` 由 Service 過濾成「使用者真的有給的那幾個」；把 ``None`` 當成
        「設為空」寫進來的話，使用者改一次名稱、描述就不見了。
        """
        return self.get_queryset().filter(id=kb_id).update(**fields)

    # ── 保留窗的硬刪（05 §5.4，二次架構審計 P0-2）──────────────
    #
    # 兩個方法都從 ``including_deleted()`` 出發——這是全 repo 少數**故意要看見已刪
    # 列**的地方，而 `SoftDeletableRepository` 要求這個意圖在呼叫端顯式可見。

    def deleted_before(self, cutoff: datetime, *, limit: int) -> list[uuid.UUID]:
        """保留窗已過的已刪 KB，最舊的先。"""
        rows = (
            self.including_deleted()
            .filter(deleted_at__lt=cutoff)
            .order_by("deleted_at")
            .values_list("id", flat=True)[:limit]
        )
        return [uuid.UUID(str(row)) for row in rows]

    def hard_delete(self, kb_ids: Sequence[uuid.UUID]) -> int:
        """真的刪掉；回傳刪掉的列數。

        **底下的文件必須先清乾淨**：`Document.kb` 是 PROTECT，還有文件時這裡會被 DB
        擋下（那個擋是對的——它保證這裡永遠不會刪出一批查不到 KB 的孤兒文件）。
        """
        if not kb_ids:
            return 0
        deleted, _ = self.including_deleted().filter(id__in=list(kb_ids)).delete()
        return int(deleted)


class DocumentRepository(SoftDeletableRepository[Document]):
    model = Document

    def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        """租戶內以 id 找文件；不存在或屬於別的租戶都回 ``None``。

        回 None 而不是 raise 是刻意的：API 層要把它轉成 **404**（09 §2.3 的資源類
        規則）。回 403 等於承認「這個 id 存在，只是你不能碰」，那讓人可以拿 id 掃出
        別的租戶有哪些文件。
        """
        return self.get_queryset().filter(id=document_id).first()

    def for_kb(self, kb_id: uuid.UUID) -> list[Document]:
        """某個 KB 底下的文件——**不是**整個租戶的文件。

        漏了 kb 條件的話，回傳的每一筆都是呼叫者有權看的資料（同租戶），所以不會有
        錯誤也不會有紅燈；使用者只會覺得「這個知識庫怎麼有別的知識庫的文件」。
        """
        return list(self.get_queryset().filter(kb_id=kb_id).order_by("-created_at"))

    def count_for_kb(self, kb_id: uuid.UUID) -> int:
        return self.get_queryset().filter(kb_id=kb_id).count()

    def for_kb_after(
        self, kb_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
    ) -> list[Document]:
        """KB 底下 id 大於游標的文件，最多 ``limit`` 份（2B-6 的重切分批）。

        **游標而不是 OFFSET**：重建期間有人上傳或刪掉一份文件，整個 OFFSET 視窗就
        位移一格，而被跳過的那份會安靜地留著舊參數切出來的 chunk。
        """
        queryset = self.get_queryset().filter(kb_id=kb_id)
        if after is not None:
            queryset = queryset.filter(id__gt=after)
        return list(queryset.order_by("id")[:limit])

    def count_in_statuses_for_kb(self, kb_id: uuid.UUID, statuses: Iterable[str]) -> int:
        """KB 底下處在這幾個狀態的文件數——重切階段用來判斷「還在跑嗎」。"""
        return self.get_queryset().filter(kb_id=kb_id, status__in=list(statuses)).count()

    # ── 保留窗的硬刪（05 §5.4，二次架構審計 P0-2）──────────────

    def purgeable(self, cutoff: datetime, *, limit: int) -> list[tuple[uuid.UUID, str]]:
        """保留窗已過、該被硬刪的文件；回傳 (id, storage_key)。

        **兩種來源，缺一不可**：使用者刪掉的文件（``deleted_at``），以及**沒有自己的
        ``deleted_at``、但 KB 已刪**的那些——`KnowledgeBaseService.delete` 刻意不逐列
        標記底下的文件（那會讓刪除變成長交易），所以「刪掉一整個 KB」在文件表上不留
        任何痕跡。只認第一種的話，KB 級刪除的資料會永遠留著，而那正是量最大的一種。

        ``storage_key`` 一起撈回來：物件要跟著刪，而列刪掉之後就沒有人知道它在哪了。
        """
        rows = (
            self.including_deleted()
            .filter(models.Q(deleted_at__lt=cutoff) | models.Q(kb__deleted_at__lt=cutoff))
            .order_by("created_at")
            .values_list("id", "storage_key")[:limit]
        )
        return [(uuid.UUID(str(row[0])), str(row[1])) for row in rows]

    def count_all_in_kb(self, kb_id: uuid.UUID) -> int:
        """KB 底下**含已刪除**的文件數——KB 能不能硬刪就看它是不是 0。"""
        return self.including_deleted().filter(kb_id=kb_id).count()

    def hard_delete(self, document_ids: Sequence[uuid.UUID]) -> int:
        """真的刪掉；回傳刪掉的列數。chunk 與 etl_job 必須先清（都是 PROTECT）。"""
        if not document_ids:
            return 0
        deleted, _ = self.including_deleted().filter(id__in=list(document_ids)).delete()
        return int(deleted)

    # ── 配額的存量聚合（04 §8.1，2A-2a）────────────────────────
    #
    # 文件數與儲存量的額度依據就是這兩個查詢——**不是 Redis 計數器**：存量必須活得
    # 比 Redis 久，且刪除要立即釋放。`get_queryset()` 已排除軟刪除（額度放的是使用
    # 者能控制的邏輯容量；物件儲存的實體位元組等清理 job）。
    #
    # **KB 被軟刪時，它底下的文件不會跟著標記**（`KnowledgeBaseService.delete` 刻意
    # 不做逐列標記，那會讓刪除變成長交易），級聯清理是 worker 的職責
    # （`DeletedKnowledgePurgeService`，保留窗過後才動）。額度不能等那麼久：少了下面
    # 這個 join 條件，「刪掉整個 KB」就完全不釋放額度——使用者撞到
    # documents:100 之後刪掉 KB、再上傳依然 429，而他已經看不到那些文件，沒有任何
    # 辦法自救。額度看的是「使用者還能不能控制的容量」，已刪 KB 底下的不算。

    def _billable(self) -> models.QuerySet[Document]:
        return self.get_queryset().filter(kb__deleted_at__isnull=True)

    def active_count(self) -> int:
        return self._billable().count()

    def active_size_bytes(self) -> int:
        total = self._billable().aggregate(total=models.Sum("size_bytes"))["total"]
        return int(total or 0)

    def find_by_content_hash(self, *, kb_id: uuid.UUID, content_hash: str) -> Document | None:
        """上傳前的去重查詢。

        查詢範圍必須與 ``uq_document_tenant_kb_hash`` 逐字對應（租戶 + KB + hash）。
        查得比約束寬的話，上傳會回報「重複」但 INSERT 其實過得了；窄的話則是回報
        可以上傳、然後被 DB 擋下並冒出 IntegrityError。兩種不一致都很難從症狀反推。
        """
        return self.get_queryset().filter(kb_id=kb_id, content_hash=content_hash).first()

    def create(
        self,
        *,
        kb_id: uuid.UUID,
        filename: str,
        mime_type: str,
        storage_key: str,
        content_hash: str,
        size_bytes: int,
        source_type: str = "upload",
        document_id: uuid.UUID | None = None,
        uploaded_by: uuid.UUID | None = None,
    ) -> Document:
        """建立文件列。

        ``document_id`` 由呼叫端指定是上傳流程的需求：物件 key 含 doc id，而物件必須
        在 DB 寫入**之前**就上傳完（見 DocumentService.upload 的順序說明）。不給時
        由 model 的 default 產生。
        """
        return Document.objects.create(
            id=document_id or uuid.uuid4(),
            tenant_id=get_current_tenant_id(operation="DocumentRepository.create"),
            kb_id=kb_id,
            filename=filename,
            mime_type=mime_type,
            storage_key=storage_key,
            content_hash=content_hash,
            size_bytes=size_bytes,
            uploaded_by=uploaded_by,
            source_type=source_type,
        )

    def stuck_in(self, statuses: Sequence[str], *, not_updated_since: datetime) -> list[Document]:
        """停在某幾個狀態、且一段時間沒有動靜的文件（維運恢復用）。

        ``not_updated_since`` 不是可有可無的參數：``uploaded`` 與 ``chunked`` 是**正常
        的過渡狀態**，剛上傳的文件本來就會在裡面待幾秒。少了時間下限，恢復指令會把
        正在被處理的文件再排一次——冪等保證那不會弄壞資料，但 embedding 那一段是真的
        錢，而重複的訊息會讓佇列在最需要吞吐的時候變成兩倍長。

        「動靜」指的是 ``updated_at``，而它由本類別的每一個 ``update()`` 顯式寫入
        （見 `set_status`）——`auto_now` 只在 `save()` 上生效。
        """
        return list(
            self.get_queryset()
            .filter(status__in=list(statuses), updated_at__lt=not_updated_since)
            .order_by("updated_at")
        )

    def start_new_version(self, document_id: uuid.UUID, *, doc_version: int) -> int:
        """re-ingest：版本 +1 並回到起點（08 §2 的 ``ready → parsing`` 那條邊）。

        版本、狀態、error 一起寫：分開寫的話，中途失敗會留下「版本已經 +1 但狀態還是
        ready」的列——下一次重跑的冪等鍵指向新版本，於是舊 chunk 永遠不會被取代。
        """
        return (
            self.get_queryset()
            .filter(id=document_id)
            .update(
                doc_version=doc_version,
                status="uploaded",
                error=None,
                updated_at=timezone.now(),
            )
        )

    def set_status(
        self, document_id: uuid.UUID, *, status: str, error: dict[str, object] | None = None
    ) -> int:
        """狀態機推進（08 §2）。``error`` 顯式傳 None 會清掉上一次的失敗紀錄。

        走 ``update`` 而不是讀出來改再存：ETL 與使用者的請求可能同時碰同一列，
        read-modify-write 會把對方的改動蓋掉（例如把已軟刪的文件寫回未刪）。

        **``updated_at`` 要自己寫**：``auto_now`` 是 `save()` 的行為，``update()``
        不會觸發它。少了這一行，一份文件從上傳到 ready 的整段處理過程中
        ``updated_at`` 都停在建立那一刻，於是所有「多久沒有動靜」的判斷（`stuck_in`
        的補償掃描、re-ingest 的卡死豁免）量到的其實是「這份文件多久以前被建立」
        ——一份剛切完塊的大文件會被誤判成停滯，而真正卡在 parsing 的反而看起來很新。
        """
        return (
            self.get_queryset()
            .filter(id=document_id)
            .update(status=status, error=error, updated_at=timezone.now())
        )


# 全文檢索的查詢（06 §3.1、05 §5.3）。**四個地方錯了都不會報錯**，見 `search_fts`。
#
# 公開（不加底線）是為了讓 `tests/integration/test_fts_retrieval.py` 拿**真正跑的這一句**
# 去 EXPLAIN：測試自己另寫一句形狀相近的 SQL 的話，兩邊漂掉時測試照樣綠，而正式路徑
# 已經在用一個 planner 選不到 pgroonga 索引的查詢。
#
# **`AS MATERIALIZED` 不是效能微調，是正確性的一部分**（2B-1 實作時實測）：把檔名的
# join 寫在同一層的話，planner 會選一條由 join 驅動的 btree 路徑（`ix_chunk_tenant_doc_seq`
# 或 `ix_chunk_tenant_kb_active`）去掃 chunk，再把 `content &@* '...'` 當成 runtime
# filter——而 `&@*`（similar search）**只在 index scan 下可用**，PGroonga 會直接拋
# `similar search available only in index scan`，也就是每一次全文檢索都 500。
# MATERIALIZED 擋掉 pushdown：CTE 內只剩「三個等值條件 + 全文」，多欄位 pgroonga 索引
# 是唯一的路；檔名的 join 於是只發生在**已經裁到 top_k 之後**的那幾列上。
FTS_SQL = """
    WITH matched AS MATERIALIZED (
        SELECT
            id,
            document_id,
            doc_version,
            content,
            meta,
            pgroonga_score(tableoid, ctid) AS score
        FROM knowledge_chunk
        WHERE tenant_id = %s
          AND kb_id = %s
          AND superseded = false
          AND content &@~ %s
        ORDER BY score DESC, id
        LIMIT %s
    )
    SELECT
        matched.id,
        matched.document_id,
        document.filename,
        matched.doc_version,
        matched.content,
        matched.meta,
        matched.score
    FROM matched
    JOIN knowledge_document AS document ON document.id = matched.document_id
    ORDER BY matched.score DESC, matched.id
"""


def _decode_meta(value: object) -> dict[str, Any]:
    """`chunk.meta` 從 **raw SQL** 回來時是字串，從 ORM 回來時是 dict。

    差別在誰做解碼：Django 的 `JSONField.from_db_value` 只在 ORM 那條路上跑，raw SQL
    拿到的是 psycopg 給的原始 jsonb 文字。兩條路都會餵進 `rag/retrievers/vector.py` 的
    `to_retrieved`，而它直接對 meta 呼叫 `.get()`——不解碼的話，症狀是 FTS 命中的
    `page` 與 `heading_path` 全部變成空的（引用面板說不出「第幾頁」），而向量那一路
    完全正常。
    """
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str) and value:
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return {}


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """檢索命中的一筆——**這是 repository 唯一允許外流的形狀**。

    不回傳 `Embedding` 或 `Chunk` 物件：那會讓上層拿到一個綁著 ORM 的東西，而
    `rag/` 依鐵則 2 不得碰 ORM。``meta`` 保持原始 dict，page 與 heading_path 的解讀
    留給知道 1D 需要什麼的那一層。

    **向量與全文兩路共用這個形狀**（2B-1 起）：`EmbeddingRepository.search` 與
    `ChunkRepository.search_fts` 回的是同一種東西，差別只在 ``score`` 怎麼算出來的
    （餘弦相似度 vs pgroonga 分數，**兩者的尺度不可互相比較**）。2B-2 的 RRF 因此
    只吃**名次**不吃分數；形狀若不一致，融合那一層就得為每一路各寫一次「這筆是從哪
    來的」。
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    # 檔名與版本在同一趟 SQL 裡撈回來（1D-5 的引用要用）：事後補查等於每則回答多 N
    # 次查詢，而引用面板是讀路徑上最頻繁的東西。join 的成本是零——`chunk__document`
    # 本來就在查詢裡。
    document_name: str
    doc_version: int
    content: str
    meta: dict[str, Any]
    score: float


class ChunkRepository(TenantScopedRepository[Chunk]):
    model = Chunk

    def purge_superseded_of_ready(self) -> int:
        """硬刪 **ready 文件**的 superseded chunk 及其向量；回傳刪掉的 chunk 數（2A-2b）。

        只挑 ready：文件還在 embedding／failed 時，舊版是唯一完整的資料，重跑失敗時
        它是最後的退路。順序是先向量後 chunk——`Embedding.chunk` 是 PROTECT，反過來
        會被 DB 擋下（那個擋是對的：它保證這裡永遠不會只刪一半）。
        """
        chunks = self.get_queryset().filter(superseded=True, document__status="ready")
        Embedding.objects.filter(chunk__in=chunks).delete()
        deleted, _ = chunks.delete()
        return deleted

    def purge_for_documents(self, document_ids: Sequence[uuid.UUID]) -> int:
        """硬刪一批文件的**全部** chunk 與向量；回傳刪掉的 chunk 數（保留窗清理用）。

        與 `purge_superseded_of_ready` 的差別是「條件」而不是「動作」：那一支挑的是
        舊版本（文件還活著），這一支的前提是**整份文件即將消失**，所以現行版也要刪。
        順序同樣是先向量後 chunk（`Embedding.chunk` 是 PROTECT）。
        """
        if not document_ids:
            return 0
        chunks = self.get_queryset().filter(document_id__in=list(document_ids))
        Embedding.objects.filter(chunk__in=chunks).delete()
        deleted, _ = chunks.delete()
        return int(deleted)

    def for_document(self, document_id: uuid.UUID) -> list[Chunk]:
        """一份文件的全部 chunk，**按 ``seq`` 排序**。

        沒有明確 ORDER BY 時 PostgreSQL 不保證順序——小表通常剛好是插入順序，於是
        開發環境看起來正常，而重寫過的表（VACUUM、re-ingest）會突然變亂序。後果是
        文件預覽語意錯亂、相鄰 chunk 拼接時接錯段落。
        """
        return list(self.get_queryset().filter(document_id=document_id).order_by("seq"))

    def search_fts(self, query: str, *, kb_id: uuid.UUID, top_k: int) -> list[ChunkHit]:
        """全文檢索：字面比對命中的 chunk 與 pgroonga 分數（06 §3.1 的 FTS 一路、2B-1）。

        **它補的是向量檢索的盲區**：向量擅長「換句話說」，對專有名詞、型號、法條編號
        卻很鈍——問「第 14 條」會回一段語氣很像但編號不對的文字，而那看起來完全像個
        答案。兩路在 2B-2 以 RRF 融合。

        四件事在這條查詢裡定案，錯了**都不會報錯**（前三個是 2B-1 開工前 spike 實測）：

        1. **運算子是 ``&@~``（查詢語法），送進來的是識別符的 OR 運算式**，不是整句
           問句（2B-2b 改）。2B-1 原本用 ``&@*``（similar search）送整句，而 2B-2 的
           評測顯示那樣更差：稀有詞單獨查得到、放進整句回 0 筆，於是 FTS 在該幫忙時
           交白卷、不該說話時投模糊票。查詢的建構與「什麼詞才算識別符」見
           `rag/retrievers/keyword.py`。
        2. **``superseded = false`` 必須寫在 SQL 裡**。索引是 partial 的
           （``ix_chunk_content_fts_active``），查詢少了同一個條件，planner 就用不到它
           ——退化成整表掃描，而結果完全正確。
        3. **``pgroonga_score()`` 在沒走索引時回 0.0**：不是 NULL、不是錯誤，是一個合法
           的分數。於是 2B-2 的 RRF 會拿到一組「全部同分」的候選，排序完全由 tie-break
           決定。第 2 點失效時的症狀就是這個，`tests/integration/test_fts_retrieval.py`
           以「分數 > 0」把兩者一起釘住。
        4. **過濾條件一個都不能少**：租戶、KB、``superseded``。少任何一個，回來的 chunk
           都「確實存在且看起來合理」——包括別的租戶的。

        ``score`` 與向量那一路的相似度**不是同一個尺度**（pgroonga 的分數沒有上界），
        兩邊的數字不可互相比較；RRF 只吃名次正是為此。

        排序帶 ``chunk.id`` 當第二鍵：同分時沒有它，兩次查詢排出來的順序可以不同，而那
        會讓引用編號在重跑時對不上（同 `rag/pipeline.py` 的 merge_candidates）。
        """
        text = query.strip()
        if not text:
            # 空查詢在 PGroonga 那裡回空集合，而那與「這個知識庫真的沒有相關內容」在
            # 結果上一模一樣——兩者對上層的處置完全不同。前處理見
            # `rag/retrievers/keyword.py` 的 `normalise_fts_query`。
            raise ValueError("全文檢索的查詢不得為空")

        if not connection.in_atomic_block:
            # RLS 讀的是交易區域參數 ``app.tenant_id``（由 `unit_of_work` 以 SET LOCAL
            # 設定）。交易外那個值不存在，policy 於是擋掉每一列——回傳空清單、沒有錯誤，
            # 而症狀是「全文檢索永遠查不到東西」。同 `EmbeddingRepository.search` 的守門。
            raise RuntimeError("ChunkRepository.search_fts 必須在交易內呼叫（RLS 的前提）")

        tenant_id = get_current_tenant_id(operation="ChunkRepository.search_fts")
        with connection.cursor() as cursor:
            # **走不到索引時 `pgroonga_score()` 會安靜地回 0.0**：不是 NULL、不是錯誤，
            # 是一個合法的分數，而 RRF 只吃名次——一組全部同分的候選，排序完全由
            # tie-break 決定。planner 在**小表**上一定選 seq scan（成本比較低），於是
            # 「剛建好、只有幾段內容的知識庫」會安靜地退化。`SET LOCAL` 把成本模型壓
            # 過去，交易結束即失效（不會外溢到連線池裡的下一個使用者，同
            # `EmbeddingRepository.search` 的 ef_search）。
            #
            # 2B-1 時這一行還有第二個作用：`&@*` 在 seq scan 下直接拋
            # NotSupportedError。2B-2b 換成 `&@~` 之後那個例外不會再出現，而「安靜的
            # 0 分」比例外更難查——所以這一行留著。
            cursor.execute("SET LOCAL enable_seqscan = off")
            try:
                cursor.execute(FTS_SQL, [str(tenant_id), str(kb_id), text, int(top_k)])
                rows = cursor.fetchall()
            finally:
                # 只讓上面那一句吃到這個設定：同一個交易裡後面還可能有別的查詢（呼叫端
                # 的 `unit_of_work` 範圍不由這裡決定），而對它們來說關掉 seq scan 只會
                # 讓 planner 選到更差的計畫。
                cursor.execute("RESET enable_seqscan")

        return [
            ChunkHit(
                chunk_id=chunk_id,
                document_id=document_id,
                document_name=filename or "",
                doc_version=int(doc_version),
                content=content,
                meta=_decode_meta(meta),
                score=float(score),
            )
            for chunk_id, document_id, filename, doc_version, content, meta, score in rows
        ]

    def for_retrieval(self, *, kb_id: uuid.UUID) -> list[Chunk]:
        """檢索候選集：該 KB 底下**未 superseded** 的 chunk。

        條件與 ``ix_chunk_tenant_kb_active``（partial index）逐字對應，查詢才吃得到
        那個索引。
        """
        return list(self.get_queryset().filter(kb_id=kb_id, superseded=False).order_by("seq"))

    def count_active_for_kb(self, *, kb_id: uuid.UUID) -> int:
        """該 KB 現行的 chunk 數——reindex 完成度的**分母**（2B-6）。

        條件與 `for_retrieval` 逐字相同（未 superseded）。兩者不一致的話，重建會照
        一組數字算進度、檢索照另一組回答，而分母偏大時那個 job 永遠到不了 100%。
        """
        return self.get_queryset().filter(kb_id=kb_id, superseded=False).count()

    def token_total_for_kb(self, *, kb_id: uuid.UUID) -> int:
        """該 KB 現行 chunk 的 token 總和——整庫重建的花費估算（2B-6 缺口④）。

        **加總 `token_count` 而不是「chunk 數 × 平均值」**：那一欄是切塊時算出來的實際
        值（1B-5 的 chunker 注入），而 chunk 長度的差距很大——用平均估的話，一個放滿
        長表格的知識庫會被低估到擋不住。

        條件與 `for_retrieval` 一致（未 superseded）：舊版 chunk 不會被重算，把它們算
        進帳單會讓一個重跑過很多次的 KB 永遠開不了重建。
        """
        total = (
            self.get_queryset()
            .filter(kb_id=kb_id, superseded=False)
            .aggregate(total=models.Sum("token_count"))["total"]
        )
        return int(total or 0)

    def active_for_version(self, *, document_id: uuid.UUID, doc_version: int) -> list[Chunk]:
        """一份文件**目前這一版、未 superseded** 的 chunk——embedding 的輸入（1C-3）。

        兩個條件缺一不可。少了 ``doc_version``，re-ingest 之後會連舊版一起算；少了
        ``superseded``，同樣會算到即將被清理 job 硬刪的那些。兩種漏法的症狀都是帳單
        變貴而資料看起來正常。
        """
        return list(
            self.get_queryset()
            .filter(document_id=document_id, doc_version=doc_version, superseded=False)
            .order_by("seq")
        )

    def mark_superseded(self, *, chunk_ids: Sequence[uuid.UUID]) -> int:
        """標記舊版本；回傳實際影響的列數。

        走 ``get_queryset()`` 出發（而非 ``Chunk.objects``）：這條 UPDATE 在 re-ingest
        時會以「整批」的形狀執行，漏了 tenant filter 就是把別的租戶的 chunk 一起標成
        superseded——受害租戶的檢索會突然回空集合，而沒有任何錯誤訊息。
        """
        return self.get_queryset().filter(id__in=list(chunk_ids)).update(superseded=True)

    def supersede_for_document(self, document_id: uuid.UUID) -> int:
        """把一份文件目前所有未 superseded 的 chunk 標成舊版（re-ingest 用）。

        **標記而不是刪除**（05 §3.2）：新版本的 embedding 還沒好，這段期間檢索仍要
        服務得了查詢。刪掉的話，重跑進行中的那幾分鐘裡這份文件會完全查不到，而使用者
        的感受是「東西不見了」。舊列由清理 job 在重嵌入完成後硬刪。
        """
        return (
            self.get_queryset()
            .filter(document_id=document_id, superseded=False)
            .update(superseded=True)
        )

    def replace_for_version(
        self,
        *,
        document_id: uuid.UUID,
        kb_id: uuid.UUID,
        doc_version: int,
        rows: Sequence[dict[str, object]],
    ) -> int:
        """先刪同版本殘留再整批寫入（08 §6 的冪等）。

        **刪除是必要的，不是保險**：``uq_chunk_document_version_seq`` 會讓重跑在
        第一筆就撞唯一約束，而部分寫入的殘留（上次跑到一半崩潰）不刪就永遠卡住。
        兩件事在同一個交易裡，中途失敗時不會留下「刪了但沒寫」的空文件。

        ``bulk_create`` 而非逐筆 ``create``：一份 500 頁的 PDF 會有上千個 chunk，
        逐筆是上千次 round-trip。
        """
        self.get_queryset().filter(document_id=document_id, doc_version=doc_version).delete()
        tenant_id = get_current_tenant_id(operation="ChunkRepository.replace_for_version")
        created = Chunk.objects.bulk_create(
            [
                Chunk(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    kb_id=kb_id,
                    doc_version=doc_version,
                    **row,
                )
                for row in rows
            ]
        )
        return len(created)


class EmbeddingRow(TypedDict):
    """一列待寫入的向量。

    ``model`` 與 ``embedding_version`` **不在這裡**：它們是整批共用的參數（一次
    Gateway 呼叫對應一個模型），放進每一列只會讓「同一批混了兩個模型」變成可能，
    而那是呼叫端組錯資料、不該由這一層容忍。
    """

    chunk_id: uuid.UUID
    vector: Sequence[float]


class EmbeddingRepository(TenantScopedRepository[Embedding]):
    """向量的讀寫與檢索（05 §3.2、06 §3.1）。"""

    model = Embedding

    def upsert(
        self,
        rows: Sequence[EmbeddingRow],
        *,
        model: str,
        embedding_version: int,
    ) -> int:
        """寫入或覆蓋一批向量；回傳處理筆數。

        走唯一約束 + ``ON CONFLICT``（Django 的 ``update_conflicts``）而不是「先查再
        寫」：重嵌入是 at-least-once 的背景工作，而併發的兩個 worker 都會查到「不存
        在」，然後其中一個撞約束失敗——那一批的整份 API 呼叫（真的錢）就白做了。

        ``model`` 與 ``embedding_version`` 是整批共用的參數而不是每列一份：一次呼叫
        對應一個模型，混在同一批寫入代表呼叫端組錯了資料。
        """
        if not rows:
            return 0

        tenant_id = get_current_tenant_id(operation="EmbeddingRepository.upsert")
        Embedding.objects.bulk_create(
            [
                Embedding(
                    tenant_id=tenant_id,
                    chunk_id=row["chunk_id"],
                    model=model,
                    embedding_version=embedding_version,
                    vector=list(row["vector"]),
                )
                for row in rows
            ],
            update_conflicts=True,
            unique_fields=["chunk", "model", "embedding_version"],
            update_fields=["vector", "updated_at"],
        )
        return len(rows)

    def for_chunks(
        self, chunk_ids: Sequence[uuid.UUID], *, model: str, embedding_version: int
    ) -> list[Embedding]:
        return list(
            self.get_queryset().filter(
                chunk_id__in=list(chunk_ids),
                model=model,
                embedding_version=embedding_version,
            )
        )

    def search(
        self,
        query_vector: Sequence[float],
        *,
        kb_id: uuid.UUID,
        model: str,
        embedding_version: int,
        top_k: int,
        ef_search: int,
    ) -> list[ChunkHit]:
        """向量檢索：回傳最相近的 chunk 與**相似度**（06 §3.1、05 §4）。

        三件事在這條查詢裡定案，錯了都不會報錯：

        1. **參數必須是 `HalfVector`**。欄位是 halfvec，而傳一般的 list 會被轉成
           `vector`；`halfvec <=> vector` 需要隱式轉型，於是 planner 用不到
           ``halfvec_cosine_ops`` 的索引——檢索退化成整表掃描，而結果完全正確。
           `tests/integration/test_vector_retrieval.py` 以 EXPLAIN 釘住這件事。

        2. **`ef_search` 用 ``SET LOCAL``**（11 §2 的 80）。它是「找多仔細」的旋鈕，
           預設值比文件定的低。用連線層級的 ``SET`` 會外溢到之後所有查詢——連線來自
           PgBouncer 的池，下一個拿到它的可能是完全不碰向量的 ETL。

        3. **回傳相似度而不是距離**（``1 - cosine_distance``）。兩者差一個負號，而都會
           排出一個看起來像答案的清單；06 §3.1 的 rerank 門檻 0.3 是相似度。

        過濾條件一個都不能少：租戶（``get_queryset``）、KB、``superseded``、以及
        model + embedding_version。少任何一個，回來的 chunk 都「確實存在且看起來合理」。
        """
        # ``SET LOCAL`` 在交易外只發一則 WARNING 就沒事了——**設定不生效，查詢照跑**，
        # 於是 ef_search 悄悄退回 PostgreSQL 的預設值（比 11 §2 定的 80 低），召回率
        # 下降而結果依然看起來完全正常。唯一的呼叫端目前在 `unit_of_work` 內，這一行
        # 把那個前提釘住，免得日後有人在交易外呼叫。
        if not connection.in_atomic_block:
            raise RuntimeError("EmbeddingRepository.search 必須在交易內呼叫（SET LOCAL 的前提）")

        with connection.cursor() as cursor:
            # 參數化而非字面值：ef_search 目前是常數，但它遲早會變成 KB 可覆寫的設定
            # （06 §3.1 的「KB 可覆寫」），而那時這裡就是使用者輸入的落點。
            #
            # `core/uow.py` 說「``SET`` 不吃查詢參數」而這裡用了 `%s`，兩處看似矛盾：
            # 差別在**誰做替換**。PostgreSQL 的 `SET` 語法確實不接受 bind parameter，
            # 但 Django 的 psycopg3 後端預設用 ``ClientCursor``（``OPTIONS`` 沒開
            # ``server_side_binding``），`%s` 在送出前就已經被替換成字面值，PostgreSQL
            # 收到的是完整的一句 SQL。`int()` 是那個替換的安全前提，不是型別潔癖。
            cursor.execute("SET LOCAL hnsw.ef_search = %s", [int(ef_search)])

        distance = CosineDistance("vector", HalfVector(list(query_vector)))
        rows = (
            self.get_queryset()
            .filter(
                chunk__kb_id=kb_id,
                chunk__superseded=False,
                model=model,
                embedding_version=embedding_version,
            )
            .annotate(distance=distance)
            .order_by("distance")
            .values_list(
                "chunk_id",
                "chunk__document_id",
                "chunk__document__filename",
                "chunk__doc_version",
                "chunk__content",
                "chunk__meta",
                "distance",
            )[:top_k]
        )
        return [
            ChunkHit(
                chunk_id=chunk_id,
                document_id=document_id,
                document_name=filename or "",
                doc_version=int(doc_version),
                content=content,
                meta=meta or {},
                score=1.0 - float(distance_value),
            )
            for (
                chunk_id,
                document_id,
                filename,
                doc_version,
                content,
                meta,
                distance_value,
            ) in rows
        ]

    def chunks_without_embedding(
        self, chunk_ids: Sequence[uuid.UUID], *, model: str, embedding_version: int
    ) -> list[uuid.UUID]:
        """這批 chunk 裡還沒有向量的那些——1C-3 的批次依據。

        少了它，重跑會把整份文件重算一次，而每個 chunk 都是一次真的 API 呼叫。
        """
        existing = set(
            self.get_queryset()
            .filter(
                chunk_id__in=list(chunk_ids),
                model=model,
                embedding_version=embedding_version,
            )
            .values_list("chunk_id", flat=True)
        )
        return [chunk_id for chunk_id in chunk_ids if chunk_id not in existing]

    def count_for_kb_version(self, *, kb_id: uuid.UUID, model: str, embedding_version: int) -> int:
        """該 KB 底下已經有**這一版**向量的 chunk 數——reindex 完成度的分子（2B-6）。

        走 ``chunk__kb_id`` 而不是 embedding 自己的欄位：向量沒有 kb_id（它掛在
        chunk 上），而反正規化第三次的代價高於這個 join。
        """
        return (
            self.get_queryset()
            .filter(chunk__kb_id=kb_id, model=model, embedding_version=embedding_version)
            .count()
        )

    def purge_other_versions(
        self, *, kb_id: uuid.UUID, keep_model: str, keep_embedding_version: int
    ) -> int:
        """刪掉該 KB 底下**除了現行版本以外**的向量（06 §2.2 第 4 步）。

        呼叫端必須先確認「已經切換過而且觀察期已過」——這個方法本身不判斷時間，
        它只認「保留哪一版」。**重建進行中時呼叫是災難**：那時的「非現行版」正是剛
        算好、還沒切換的那一批，刪掉會讓那個 job 永遠到不了 100%，而它每一輪都重算
        一次、每一輪都被刪一次（見 `OldEmbeddingCleanupService` 的守門）。
        """
        deleted, _ = (
            self.get_queryset()
            .filter(chunk__kb_id=kb_id)
            .exclude(model=keep_model, embedding_version=keep_embedding_version)
            .delete()
        )
        return int(deleted)


class KbReindexJobRepository(TenantScopedRepository[KbReindexJob]):
    """KB 級重建的 job（06 §2.2、2B-6）。"""

    model = KbReindexJob

    def create(
        self,
        *,
        kb_id: uuid.UUID,
        target_model: str,
        target_embedding_version: int,
        target_knowledge_version: int,
        rechunk: bool,
        total_chunks: int,
        total_documents: int,
    ) -> KbReindexJob:
        """建立。**併發的第二筆由 DB 的 partial unique 擋下**（不是這裡的 if）。

        呼叫端要接 `IntegrityError` 並轉成 409：使用者在等 40 分鐘時會再按一次，
        而兩個請求會同時通過任何「先查再建」的檢查。
        """
        return KbReindexJob.objects.create(
            tenant_id=get_current_tenant_id(operation="KbReindexJobRepository.create"),
            kb_id=kb_id,
            target_model=target_model,
            target_embedding_version=target_embedding_version,
            target_knowledge_version=target_knowledge_version,
            rechunk=rechunk,
            total_chunks=total_chunks,
            total_documents=total_documents,
        )

    def get_by_id(self, job_id: uuid.UUID) -> KbReindexJob | None:
        return self.get_queryset().filter(id=job_id).first()

    def latest_for_kb(self, kb_id: uuid.UUID) -> KbReindexJob | None:
        return self.get_queryset().filter(kb_id=kb_id).order_by("-created_at").first()

    def active_for_kb(self, kb_id: uuid.UUID, *, statuses: Sequence[str]) -> KbReindexJob | None:
        """進行中的 job。

        ``statuses`` 由呼叫端傳入而不是在這裡寫死：那組字串的單一來源是
        `services/knowledge/reindex_plan.REINDEX_ACTIVE_STATUSES`，而 repository
        不得 import services（鐵則 2 的方向是 services → repositories）。在這裡再寫
        一份的話，兩份遲早會漂，而漂掉時的症狀是「擋不住第二次觸發」——沒有錯誤，
        只有兩個 job 互相覆蓋。
        """
        return self.get_queryset().filter(kb_id=kb_id, status__in=list(statuses)).first()

    def update(self, job_id: uuid.UUID, **fields: object) -> int:
        """部分更新，**並且一定推 `updated_at`**。

        `updated_at` 是 `auto_now`，而那只在 `save()` 時生效——`QuerySet.update()`
        完全不碰它（`DocumentRepository.start_new_version` 也為此手動帶時間戳）。
        少了這一行，每一次進度回寫都不會讓 job 看起來「有動靜」，於是
        `StuckReindexRescueService` 會把每一個**正在正常跑**的重建判成停滯——症狀是
        「重建到一半突然失敗」，而它看起來像 provider 的問題。
        """
        fields.setdefault("updated_at", timezone.now())
        return self.get_queryset().filter(id=job_id).update(**fields)

    def stuck_in(
        self, statuses: Sequence[str], *, not_updated_since: datetime
    ) -> list[KbReindexJob]:
        """停超過門檻的 job（補償掃描的輸入）。

        **時間下限不可省**：沒有它，剛送出去、訊息還在飛的 job 每一輪都會被補送一次。
        """
        return list(
            self.get_queryset()
            .filter(status__in=list(statuses), updated_at__lt=not_updated_since)
            .order_by("updated_at")
        )

    def switched_before(self, cutoff: datetime, *, limit: int) -> list[KbReindexJob]:
        """切換超過觀察期、而且還沒清過舊向量的 job（第 4 步的輸入）。

        條件是 ``switched_at``（不是 ``created_at``）：可回退的窗口從**切換**那一刻
        起算，用建立時間算的話，重建跑得愈久可回退的時間愈短——而跑得久的正是最該
        留退路的那些。
        """
        return list(
            self.get_queryset()
            .filter(status="completed", purged_at__isnull=True, switched_at__lt=cutoff)
            .order_by("switched_at")[:limit]
        )


class EtlJobRepository(TenantScopedRepository[EtlJob]):
    model = EtlJob

    def find(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob | None:
        """依冪等鍵定位（08 §6）。三個欄位缺一不可，理由見本檔 docstring。"""
        return (
            self.get_queryset()
            .filter(document_id=doc_id, doc_version=doc_version, stage=stage)
            .first()
        )

    def create(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob:
        return EtlJob.objects.create(
            tenant_id=get_current_tenant_id(operation="EtlJobRepository.create"),
            document_id=doc_id,
            doc_version=doc_version,
            stage=stage,
        )

    def start(self, *, doc_id: uuid.UUID, doc_version: int, stage: str) -> EtlJob:
        """取得或建立這個階段的 job，並標記為執行中、attempt +1。

        ``get_or_create`` 走 DB 的唯一約束（08 §6 的冪等鍵）而不是「先查再建」：
        併發觸發（使用者連點兩次、重試與排程同時進來）時，先查再建的兩邊都會查到
        「不存在」，於是各自建一筆——而那兩個 job 會同時處理同一份文件。

        ``attempt`` 用 ``F()`` 而非讀出來加一再存回去（同
        `repositories/identity.py` 的 `bump_token_version`）：後者在上面那個併發情境下
        會互相覆蓋，兩次執行只加了一次。而這個欄位是 08 §6「重試 ≤3」的依據——少算
        一次就是多跑一次，而多跑的那一次沒有任何症狀，只是重試上限悄悄變成 4。
        """
        job, _ = EtlJob.objects.get_or_create(
            tenant_id=get_current_tenant_id(operation="EtlJobRepository.start"),
            document_id=doc_id,
            doc_version=doc_version,
            stage=stage,
        )
        now = timezone.now()
        # `updated_at` 顯式寫入：``.update()`` 不會觸發 ``auto_now``（那是 `save()` 的
        # 行為），少了它「這個 job 最後被動過的時間」會停在建立的當下。
        self.get_queryset().filter(id=job.id).update(
            status="running",
            attempt=models.F("attempt") + 1,
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        job.refresh_from_db()
        return job

    def finish(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        stats: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> int:
        """收尾（succeeded / failed）。``finished_at`` 一併寫，避免兩處各寫一半。"""
        return (
            self.get_queryset()
            .filter(id=job_id)
            .update(
                status=status,
                stats=stats or {},
                error=error,
                finished_at=timezone.now(),
            )
        )

    def purge_for_documents(self, document_ids: Sequence[uuid.UUID]) -> int:
        """硬刪一批文件的 job 紀錄；回傳刪掉的列數（保留窗清理用）。

        `EtlJob.document` 是 PROTECT，不先清這裡的話文件刪不掉——而症狀是清理 job
        每天都跑、每天都在同一批文件上被 DB 擋下，表繼續長。
        """
        if not document_ids:
            return 0
        deleted, _ = self.get_queryset().filter(document_id__in=list(document_ids)).delete()
        return int(deleted)
