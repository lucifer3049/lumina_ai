"""`/rag` 端點（09 §2.3）。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。檢索的邏輯
一行都不在這裡。

**這個端點不生成答案**，只回「找到哪些 chunk、分數多少」。存在的理由有兩個，而兩個都
與 1D 無關：整合方需要一個純檢索介面（09 §2.3 明列）；而在 Chat 做完之前，它是唯一能
實際看到「檢索到底準不準」的入口——沒有它，檢索品質要等一整個工作包之後才第一次被人
看見，而那時要分辨「檢索爛」與「生成爛」已經困難得多。

**權限是 `rag:query` 而不是 `knowledge:read`**：檢索每一次都要花錢，而「能看文件清單」
與「能無限次觸發付費呼叫」是兩件事（見 `services/rag/permissions.py`）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.problem import ERROR_RESPONSES
from api.schemas.rag import RagQueryIn, RagQueryOut, RetrievedChunkOut
from core.db import run_orm
from rag.retrievers.vector import RetrievedChunk
from services.rag.retrieval import RetrievalService

router = APIRouter(tags=["rag"], responses=ERROR_RESPONSES)
_retrieval = RetrievalService()


def _chunk_out(chunk: RetrievedChunk) -> RetrievedChunkOut:
    return RetrievedChunkOut(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_name=chunk.document_name,
        doc_version=chunk.doc_version,
        content=chunk.content,
        score=chunk.score,
        page=chunk.page,
        heading_path=chunk.heading_path,
    )


@router.post("/rag/query", operation_id="rag_query")
async def rag_query(
    body: RagQueryIn,
    principal: Annotated[Principal, Depends(RequireScope("rag:query"))],
) -> RagQueryOut:
    """檢索（不生成）。查不到東西是 200 + 空清單，不是 404。

    404 的意思是「這個 KB 不存在」，而「存在但沒有相關內容」是完全不同的情況——
    1D 對後者有自己的處置（06 §3.1：回「知識庫無相關內容」而不是硬答）。
    """
    results = await run_orm(
        _retrieval.query,
        principal.tenant_id,
        kb_id=body.kb_id,
        query=body.query,
        top_k=body.top_k,
    )
    return RagQueryOut(items=[_chunk_out(chunk) for chunk in results])
