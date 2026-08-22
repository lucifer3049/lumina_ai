"""PromptBuilder —— 模板渲染與資料分域（04 §5.3、06 §3.5、10 §5、1D-3b）。

鐵則 5 的後半段：**Prompt 一律經這裡使用版本化模板，禁止散落 Python 字串**。模板本身
存在 DB（`apps/ai/models.py`），這個模組只負責「模板字串 + 變數 → 最終文字」，以及
「檢索到的 chunk → 一段有邊界的資料」。

**兩件事刻意分開，而分開的理由是安全而不是整潔**：

- 模板是**租戶可編輯的資產**（09 §2.5 的 `/prompts`，Phase 5），所以渲染跑在沙箱裡。
- context 的定界標記**不是模板的一部分**，是這裡寫死的程式碼。它是 10 §5 的 injection
  防線本身——可編輯的話，一個把定界拿掉的模板會讓防線消失，而症狀是「偶爾會照著
  文件裡的指示做」，沒有人會把它連回模板的那次修改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import jsonschema
from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError

# `SecurityError` 從 `jinja2.exceptions` 取：`jinja2.sandbox` 只是把它 re-export，
# 而它不在那個模組的 `__all__` 裡（mypy strict 會擋下隱式的 re-export）。
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

from core.exceptions import ValidationFailedError

__all__ = [
    "CONTEXT_END",
    "CONTEXT_START",
    "ContextChunk",
    "build_context_block",
    "build_user_turn",
    "render_template",
    "validate_variables",
]

# context 的定界標記。挑成不會出現在正常文件裡的形狀（大寫、角括號、底線），因為
# 「碰巧出現」與「刻意偽造」在下游是同一件事——見 `build_context_block` 的中和處理。
CONTEXT_START = "<<<KNOWLEDGE_CONTEXT>>>"
CONTEXT_END = "<<<END_KNOWLEDGE_CONTEXT>>>"

# 偽造的標記換成這個。**不刪掉、不改寫其餘文字**：1D-5 的引用要能對回原文，而
# 「這段文字裡有一個長得像我們標記的東西」本身不是錯誤，只是不能保留它的結構作用。
_DEFUSED = "<<redacted-delimiter>>"

# `SandboxedEnvironment`：屬性存取受限，`{{ ''.__class__ }}` 這類逃逸會被擋下。
# `StrictUndefined`：未定義的變數要**炸**，而不是渲染成空字串（Jinja2 的預設）——
# 空字串的症狀是「LLM 開始自由發揮」，與檢索沒命中長得一模一樣。
# `autoescape=False`：這裡產出的是 prompt 不是網頁；escape 會把 context 裡的 `<`
# 與 `&` 變成 HTML 實體，而引用驗證要拿回答對回原文。
_ENV = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


@dataclass(frozen=True, slots=True)
class ContextChunk:
    """要放進 context 的一段檢索結果。

    `marker` 進 `[c:...]` 標記（引用契約），`doc_name`、`page` 與 `heading_path` 讓
    模型答得出「依據員工手冊第 3 頁」，也讓 06 §3.3 的引用面板有東西可顯示。

    **`marker` 由呼叫端決定，這一層不認識 `chunk_id`。** 1D-5 用的是「本輪第幾段」
    的短編號而不是 UUID（偏離 06 §3.1，記於 13 §3.5）：一個 UUID 約 20 token 而模型
    每引用一次抄一遍、輸出 token 又比輸入貴數倍；而叫模型一字不差抄 36 個十六進位
    字元，它會抄錯——抄錯就被驗證當成幻覺剔掉，畫面上少一個本來是真的來源。
    對映回真 `chunk_id` 是 `rag/citation.py` 的事。
    """

    marker: str
    text: str
    doc_name: str = ""
    page: int | None = None
    heading_path: tuple[str, ...] | list[str] = ()


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """渲染一份模板。失敗一律是 `ValidationFailedError`（呼叫端轉得成 422）。

    三種失敗分開處理，因為它們的**訊息可以說多少**不同：

    - 少變數：訊息要說出是哪一個。模板有十個變數時，「有一個沒給」等於什麼都沒說。
    - 語法錯：說得出行號對寫模板的人有用。
    - 沙箱擋下：**只給固定字串**。Jinja2 的原文會夾著我們的模組路徑與物件結構，而
      那段訊息會經 API 回到編輯模板的人手上——對寫模板的人沒有幫助，對攻擊者是地圖。
    """
    try:
        return _ENV.from_string(template).render(dict(variables))
    except UndefinedError as exc:
        raise ValidationFailedError(f"模板變數未提供：{exc}") from exc
    except SecurityError as exc:
        raise ValidationFailedError("模板使用了不允許的存取（沙箱）") from exc
    except TemplateSyntaxError as exc:
        raise ValidationFailedError(f"模板語法錯誤（第 {exc.lineno} 行）") from exc


def validate_variables(schema: Mapping[str, Any], variables: Mapping[str, Any]) -> None:
    """依 `variables_schema`（JSON Schema，05 §3.3）驗證變數。

    **比 JSON Schema 嚴格一項：未宣告的變數視為錯誤。** JSON Schema 預設允許額外欄位，
    但在這裡多給的變數幾乎都是打錯字（`questoin`）——而打錯字的真正後果是「模板要的
    那個沒給到」，也就是上面 `StrictUndefined` 要擋的東西。放行等於把一個看得見的錯誤
    變成一個看不見的。
    """
    if not schema:
        return

    declared = set(schema.get("properties", {}))
    if declared:
        unknown = sorted(set(variables) - declared)
        if unknown:
            raise ValidationFailedError(f"未宣告的模板變數：{'、'.join(unknown)}")

    try:
        jsonschema.validate(dict(variables), dict(schema))
    except jsonschema.ValidationError as exc:
        # `exc.message` 是給人看的那一句（例如 "'question' is a required property"）；
        # 完整的 exc 會把整份 schema 與實例 dump 出來，而變數裡可能有使用者的問句。
        raise ValidationFailedError(f"模板變數不符 schema：{exc.message}") from exc
    except jsonschema.SchemaError as exc:
        raise ValidationFailedError(f"模板的 variables_schema 本身不合法：{exc.message}") from exc


def build_context_block(chunks: Sequence[ContextChunk]) -> str:
    """把檢索結果包成一段**有邊界的資料**（10 §5：指令與資料分域）。

    順序即相關性（1C-4 的 top_k 排序），照原順序輸出——打亂的話，最相關的那一段會
    落在 context 中段，而長 context 的中段正是模型最容易忽略的位置。

    空清單仍然產出完整結構：檢索一筆都沒命中是正常情況（06 §3.1 的門檻），而回一個
    空字串會讓 system 說「只依據以下內容」後面什麼都沒有——那是 hallucination 最好的
    溫床。有結構，模型才看得出「這裡本來就沒有東西」。
    """
    lines = [CONTEXT_START]
    for chunk in chunks:
        lines.append(_source_line(chunk))
        lines.append(_defuse(chunk.text))
        lines.append("")
    lines.append(CONTEXT_END)
    return "\n".join(lines)


def build_user_turn(question: str, context: str = "") -> str:
    """這一輪要送進 `role="user"` 的完整內容（06 §3 的組裝順序）。

    **順序是 context 在前、問題在後**，兩個理由：模型讀到問題時要已經看過資料；而把
    問題埋在一大段 context 之前會讓 prompt cache 的穩定前綴（06 §4）從第一個 token
    就失效——每一輪都是一次全新的 prompt。

    **context 進 user 而不是 system**（10 §5 的指令／資料分域）：混在同一個 role 裡
    的話，一份被污染的文件就能用「忽略以上指令」改寫我們的規則，而回答看起來完全
    正常。這個位置的選擇與定界標記是同一道防線的兩半。

    沒有 context（純閒聊路徑，06 §9）時就只有問題——**不放一個空的資料區塊**：那會
    讓模型以為「查過了、沒東西」，而實際上是這場對話根本沒掛知識庫。
    """
    if not context:
        return question
    return f"{context}\n\n問題：{question}"


def _source_line(chunk: ContextChunk) -> str:
    source = chunk.doc_name or "未命名文件"
    if chunk.page is not None:
        source = f"{source} 第 {chunk.page} 頁"
    # 章節路徑是 Markdown 與 xlsx 唯一說得出位置的東西——那兩種來源沒有頁碼，
    # 只給檔名的話，模型與引用面板都只能說「出自某某檔案」。
    if chunk.heading_path:
        source = f"{source}（{' › '.join(chunk.heading_path)}）"
    return f"[c:{chunk.marker}] 來源：{source}"


def _defuse(text: str) -> str:
    """中和 chunk 內容裡偽造的定界標記。

    **這是 indirect injection 最直接的一條路**（10 §5）：一份被污染的文件只要含有我們
    的結束標記，後面的文字看起來就像落在資料區塊之外——也就是變成指令。

    只換標記本身，其餘一字不動：刪改內容會讓 1D-5 的引用對不回原文，而一段文字是不是
    攻擊要看它在哪一區，不是看它寫了什麼。

    **只中和「逐字相同」的標記，這是刻意的邊界而不是漏做**（2026-08-22 審查記錄）：
    帶零寬字元或多一個空格的仿冒標記過得了這一關，因為模型看的是「長得像」而不是
    「逐字相等」。往下追（正規化、清掉零寬字元、比對相似度）是一條沒有終點的路——
    每加一種變形，攻擊者就換下一種，而每一次正規化都在改動要被引用的原文。

    所以定界**不是**這裡的主要防線，它只負責「一眼看得出的那一種」。真正撐住的是另外
    兩件事，兩件都已經在位：**role 分域**（文件內容一律進 user 訊息，指令只在 system
    ——見 `build_user_turn` 的說明）與 **system 模板的規則**（明訂只依據資料區塊回答）。
    這一層失手時，那兩層仍然成立。
    """
    return text.replace(CONTEXT_START, _DEFUSED).replace(CONTEXT_END, _DEFUSED)
