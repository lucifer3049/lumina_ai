"""`ExtractedDoc` 的序列化 —— 階段之間的中間產物（08 §2、§7）。

08 §2 的狀態機在 `cleaned` 這一步把 `ExtractedDoc` 落到物件儲存，換到的是**斷點續跑**：
chunk 階段失敗重跑時不必重新解析一次 PDF（那是整條鏈最貴、也最可能逾時的一段）。
代價是多一次 IO——08 §7 已經評估過，值得。

用 JSON 而不是 pickle：這份東西會躺在物件儲存裡跨版本存在，而 pickle 綁死類別的
形狀與模組路徑（改個欄位名，舊檔就再也讀不回來），還能在反序列化時執行任意程式碼。
JSON 的欄位缺漏是可以處理的錯誤，不是遠端執行。

**這裡不碰物件儲存**：`etl/` 是純轉換層，讀寫由 service 負責（鐵則 2）。
"""

from __future__ import annotations

import json
from typing import Any

from core.exceptions import ExtractionFailedError
from etl.extract.model import Block, BlockMeta, BlockType, ExtractedDoc

# 序列化格式版本。改變 block 結構時 +1，讀到不認得的版本就當中間產物不可用而重跑
# ——那是安全的（重跑只是多花時間），而硬讀一份形狀不同的舊檔會產生看似正常的錯資料。
FORMAT_VERSION = 1


def to_json(doc: ExtractedDoc) -> str:
    payload = {
        "format_version": FORMAT_VERSION,
        "doc_meta": doc.doc_meta,
        "blocks": [
            {
                "type": str(block.type),
                "text": block.text,
                "order": block.meta.order,
                "page": block.meta.page,
                "heading_path": list(block.meta.heading_path),
            }
            for block in doc.blocks
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def from_json(data: str | bytes) -> ExtractedDoc:
    """反序列化。格式不符一律 `ExtractionFailedError`，由呼叫端決定重跑。"""
    try:
        payload: dict[str, Any] = json.loads(data)
        if payload.get("format_version") != FORMAT_VERSION:
            raise ExtractionFailedError(
                f"中間產物格式版本不符（{payload.get('format_version')}）",
                cause="artifact_version_mismatch",
            )
        blocks = tuple(
            Block(
                type=BlockType(item["type"]),
                text=item["text"],
                meta=BlockMeta(
                    order=item["order"],
                    page=item["page"],
                    heading_path=tuple(item["heading_path"]),
                ),
            )
            for item in payload["blocks"]
        )
    except ExtractionFailedError:
        raise
    except Exception as exc:
        raise ExtractionFailedError(
            "中間產物讀取失敗",
            cause=type(exc).__name__,
        ) from exc
    return ExtractedDoc(blocks=blocks, doc_meta=dict(payload["doc_meta"]))
