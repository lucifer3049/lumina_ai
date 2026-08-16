"""Cursor 分頁的游標編解碼（09 §1.1）。

**游標必須是不透明的。** 直接回一個 id 或位移的話，client 遲早會自己拼一個，而那個
格式就變成 API 契約的一部分——之後改排序鍵會打破所有既有的呼叫端，而且是在他們的
程式碼裡壞掉，我們的測試不會有任何反應。

編碼只是 base64（**不是加密**）：目的是「看起來不像可以自己組的東西」，不是保密。
游標內容本來就是使用者自己那一頁的排序值，沒有機密性。

`common/` 不 import 任何其他層（鐵則 2），因此這裡只認得字串與時間。
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

__all__ = ["CursorError", "decode_cursor", "encode_cursor"]


class CursorError(ValueError):
    """游標解不開。

    **呼叫端要轉成 422 而不是當成第一頁**：當成第一頁的話，前端的無限捲動會永遠停在
    開頭而看起來像「載不完」；而回 500 是把 client 的錯誤記成我們的錯誤。
    """


def encode_cursor(values: dict[str, Any]) -> str:
    """把排序鍵編成游標。時間一律轉 ISO 字串（JSON 沒有 datetime）。"""
    payload = {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in values.items()
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """解開游標；任何形式的壞掉都是 `CursorError`。

    壞掉的來源不只有惡意——舊版前端存在 localStorage 的游標、使用者手動改網址、
    複製貼上時被截斷，都會走到這裡。因此**不能假設格式正確**，而且錯誤要是可預期的
    一種，不是 `binascii` 或 `json` 冒出來的例外。
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CursorError("游標格式不正確") from exc
    if not isinstance(payload, dict):
        raise CursorError("游標內容不正確")
    return payload
