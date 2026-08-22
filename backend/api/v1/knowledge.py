"""`/knowledge-bases` 與 `/documents` 端點（09 §2.3）。

鐵則 3（Controller 三行原則）：解析請求 → 呼叫一個 Service 方法 → 回傳。

**權限宣告寫在每個端點上**（10 §3）：讀這個檔案就知道每條路需要什麼權限。分級的
依據是**破壞範圍**，不是動作類型：

- ``knowledge:read``——看。全部四個角色都有；Viewer 看不到文件清單的話，連「這個
  答案引用的是哪份文件」都無從確認。
- ``knowledge:write``——文件進出。Editor 的日常工作，破壞範圍限單一文件且是軟刪除。
- ``knowledge:admin``——建立/刪除 KB、改設定。刪 KB 連帶整庫文件失效，改 config 會
  讓整個知識庫需要重算——維運等級的動作。

兩個 router 合在一份檔案：它們是同一個 bounded context 的對外面，而 `/documents`
的路徑刻意**不掛在 KB 底下**（文件 id 已經唯一，巢狀路徑會逼 client 記住它屬於哪個
KB，而那在文件搬移時就錯了）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status

from api.dependencies.auth import Principal
from api.dependencies.permissions import RequireScope
from api.schemas.knowledge import (
    DocumentListOut,
    DocumentOut,
    KnowledgeBaseCreateIn,
    KnowledgeBaseListOut,
    KnowledgeBaseOut,
    KnowledgeBaseUpdateIn,
)
from api.schemas.problem import ERROR_RESPONSES
from core.db import run_orm
from services.knowledge.documents import DocumentService, DocumentView
from services.knowledge.knowledge_bases import KnowledgeBaseService, KnowledgeBaseView

router = APIRouter(tags=["knowledge"], responses=ERROR_RESPONSES)
_knowledge_bases = KnowledgeBaseService()
_documents = DocumentService()


def _kb_out(view: KnowledgeBaseView) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=view.id,
        name=view.name,
        description=view.description,
        status=view.status,
        document_count=view.document_count,
    )


def _document_out(view: DocumentView) -> DocumentOut:
    return DocumentOut(
        id=view.id,
        kb_id=view.kb_id,
        filename=view.filename,
        mime_type=view.mime_type,
        size_bytes=view.size_bytes,
        status=view.status,
        doc_version=view.doc_version,
        error=view.error,
    )


# ── 知識庫 ──────────────────────────────────────────────────────


@router.get("/knowledge-bases", operation_id="knowledge_bases_list")
async def list_knowledge_bases(
    principal: Annotated[Principal, Depends(RequireScope("knowledge:read"))],
) -> KnowledgeBaseListOut:
    views = await run_orm(_knowledge_bases.list_knowledge_bases, principal.tenant_id)
    return KnowledgeBaseListOut(items=[_kb_out(view) for view in views])


@router.post(
    "/knowledge-bases",
    operation_id="knowledge_bases_create",
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    payload: KnowledgeBaseCreateIn,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:write"))],
) -> KnowledgeBaseOut:
    """建立 KB 是 ``knowledge:write``，刪除與改設定才是 ``admin``。

    不對稱是刻意的：多一個空知識庫沒有破壞性，而 Editor 建不了 KB 的話，每次要新
    主題都得找管理員——那會讓人把所有東西倒進同一個 KB，檢索品質跟著爛掉。
    """
    view = await run_orm(
        _knowledge_bases.create,
        principal.tenant_id,
        name=payload.name,
        description=payload.description,
    )
    return _kb_out(view)


@router.get("/knowledge-bases/{kb_id}", operation_id="knowledge_bases_get")
async def get_knowledge_base(
    kb_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:read"))],
) -> KnowledgeBaseOut:
    return _kb_out(await run_orm(_knowledge_bases.get, principal.tenant_id, kb_id))


@router.patch("/knowledge-bases/{kb_id}", operation_id="knowledge_bases_update")
async def update_knowledge_base(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdateIn,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:admin"))],
) -> KnowledgeBaseOut:
    view = await run_orm(
        _knowledge_bases.update,
        principal.tenant_id,
        kb_id,
        name=payload.name,
        description=payload.description,
    )
    return _kb_out(view)


@router.delete(
    "/knowledge-bases/{kb_id}",
    operation_id="knowledge_bases_delete",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:admin"))],
) -> None:
    await run_orm(_knowledge_bases.delete, principal.tenant_id, kb_id)


# ── 文件 ────────────────────────────────────────────────────────


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    operation_id="documents_upload",
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="單請求上傳上限 32MB，超過走分塊直傳")],
    principal: Annotated[Principal, Depends(RequireScope("knowledge:write"))],
) -> DocumentOut:
    """單請求 multipart 上傳（09 §3.1 的小檔路徑）。

    **讀檔在這裡、判定在 service**：讀取 request body 屬於「解析請求」（鐵則 3 允許），
    而「這份內容能不能收」是業務規則。

    ``file.content_type`` 刻意不使用——那是 client 自報的字串（見
    `services/knowledge/uploads.py`）。檔名同理只當顯示用，實際型別由內容決定。
    """
    content = await file.read()
    view = await run_orm(
        _documents.upload,
        principal.tenant_id,
        kb_id,
        filename=file.filename or "untitled",
        content=content,
        # 上傳者（2A-5）：ETL 失敗與 ready 的通知都寄給他。
        uploaded_by=principal.user_id,
    )
    return _document_out(view)


@router.get("/knowledge-bases/{kb_id}/documents", operation_id="documents_list")
async def list_documents(
    kb_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:read"))],
) -> DocumentListOut:
    views = await run_orm(_documents.list_for_kb, principal.tenant_id, kb_id)
    return DocumentListOut(items=[_document_out(view) for view in views])


@router.get("/documents/{document_id}", operation_id="documents_get")
async def get_document(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:read"))],
) -> DocumentOut:
    return _document_out(await run_orm(_documents.get, principal.tenant_id, document_id))


@router.post(
    "/documents/{document_id}/reingest",
    operation_id="documents_reingest",
    # 202：ETL 是非同步的（09 §2.3）。回 200 會讓 client 以為重跑已經完成，而它
    # 才剛進佇列——前端該做的是回去輪詢狀態，狀態碼就是那個提示。
    status_code=status.HTTP_202_ACCEPTED,
)
async def reingest_document(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:write"))],
) -> DocumentOut:
    return _document_out(await run_orm(_documents.reingest, principal.tenant_id, document_id))


@router.delete(
    "/documents/{document_id}",
    operation_id="documents_delete",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(RequireScope("knowledge:write"))],
) -> None:
    await run_orm(_documents.delete, principal.tenant_id, document_id)
