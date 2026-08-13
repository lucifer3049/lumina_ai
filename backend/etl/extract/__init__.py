"""Extract 階段：來源位元組 → `ExtractedDoc`（08 §3）。

對外只有一個函式 `extract`，與一張「media type → loader」的註冊表。**新增來源 =
新增一個 loader 並登記一行**（08 §1）——這是這一層唯一該有的擴充方式；把格式判斷寫成
散在各處的 if/else，下游遲早會開始依賴「它是不是 PDF」。

判定用的 media type 來自上傳端的內容嗅探（`services/knowledge/uploads`，依 magic
bytes 而非副檔名），常數共用於 `core/media_types`。
"""

from __future__ import annotations

from core.exceptions import ExtractionFailedError
from core.media_types import DOCX, PDF, TEXT
from etl.extract.loaders import Loader
from etl.extract.loaders import docx as docx_loader
from etl.extract.loaders import pdf as pdf_loader
from etl.extract.loaders import text as text_loader
from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc

__all__ = ["Block", "BlockMeta", "BlockType", "ExtractedDoc", "extract"]

_LOADERS: dict[str, Loader] = {
    PDF: pdf_loader.load,
    DOCX: docx_loader.load,
    TEXT: text_loader.load,
}


def extract(content: bytes, *, media_type: str) -> ExtractedDoc:
    """依 ``media_type`` 選 loader 並抽取。失敗一律 `ExtractionFailedError`。

    **第三方例外在這裡收斂。** pdfplumber / python-docx 的例外型別會隨版本改變，而
    08 §6 的重試判定要有穩定依據；``details["cause"]`` 保留原始型別名供分類。

    這個函式**不做**行程隔離——隔離是呼叫端的決定（見 `etl.extract.sandbox`）：
    單元測試與工具腳本直接呼叫最省事，而 worker 一律經 sandbox。把 fork 寫死在
    這裡的話，前者每跑一個案例就多一次 fork，後者也失去調整上限的餘地。
    """
    loader = _LOADERS.get(media_type)
    if loader is None:
        raise ExtractionFailedError(
            f"沒有對應的 loader：{media_type}",
            cause="unsupported_media_type",
            details={"media_type": media_type},
        )

    try:
        blocks, source_meta = loader(content)
    except ExtractionFailedError:
        raise
    except Exception as exc:
        raise ExtractionFailedError(
            f"抽取失敗（{media_type}）",
            cause=type(exc).__name__,
        ) from exc

    doc_meta = {
        **source_meta,
        # 共同鍵最後覆蓋：loader 不小心填了同名的鍵時，下游看到的仍是這裡算的值。
        "media_type": media_type,
        "block_count": len(blocks),
    }
    return ExtractedDoc(blocks=tuple(blocks), doc_meta=doc_meta)
