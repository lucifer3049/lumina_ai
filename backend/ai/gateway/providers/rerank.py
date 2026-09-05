"""Rerank adapter —— 自架 TEI 與 Jina（06 §3.1／§3.4、13 §4 工作包 2B-4）。

**兩家不共用一個實作**，這與 `openai_compatible.py` 的「五家共用一個 adapter」是相反
的決定，理由也相反：那五家講的是同一種 REST（`/embeddings`、同樣的欄位名、同樣的回應
形狀），差別只有位址與金鑰；rerank 沒有這種共通形狀——TEI 收 `texts`、回一個裸陣列且
不回報模型名，Jina 收 `documents`、回 `{"results": [{"index", "relevance_score"}]}`。
硬塞進一張廠商表的話，表裡會開始長「回應是不是陣列」「分數欄位叫什麼」這種欄位，那
不是資料，是把兩個實作寫在一個 if 裡。

**但規則只有一份**（`_RerankClient`）：逾時怎麼傳、錯誤怎麼分類、金鑰怎麼藏、索引怎麼
驗——那些與哪一家無關，而寫兩份的話，其中一份寫錯只在切換到那家時才走得到。

**兩家的定位不同**（13 §4 的 2B 開工前定案）：TEI 是主線（每次提問都要打一次，付費
API 的帳單隨查詢量線性長，而本專案不商業化）；Jina 是第二個 adapter，證明 Gateway 沒
綁死一家，也讓沒有 GPU 的機器有東西可用。

**不重試、失敗往上拋**（2B-3 的規則，見 `AIGateway.rerank`）：這一層只做翻譯。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx

from ai.gateway.providers import ProviderRerank, RerankedDocument
from core.exceptions import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = [
    "JINA_BASE_URL",
    "TEI_DEFAULT_BASE_URL",
    "JinaRerankProvider",
    "TeiRerankProvider",
]

# 自架 TEI 的預設位址。**給一個預設而不是強制設定**：TEI 依定義跑在自己的機器上
# （`make tei-up` 起的就是這個 port），而設定漏了的後果是連線被拒——那是一個看得見的
# 失敗，且會被降級鏈接住。形狀同 `VENDORS["vllm"]` 的 `127.0.0.1:8000`。
TEI_DEFAULT_BASE_URL = "http://127.0.0.1:8080"
JINA_BASE_URL = "https://api.jina.ai/v1"


@lru_cache(maxsize=8)
def _shared_client(base_url: str, transport: httpx.BaseTransport | None) -> httpx.Client:
    """一個位址一組連線池（同 `openai_compatible._shared_client`，理由也相同）。

    rerank 比 embedding 更需要它：**每一次提問都要打一次**，而每次新建 client 就是每次
    重付一輪 TCP + TLS 握手——那 100~300ms 直接落在 1.2s 的預算裡，也就是直接決定
    rerank 會不會被跳過。

    **鍵含 transport**：測試注入 `httpx.MockTransport`，共用同一個 client 的話，上一條
    測試的假回應會服務下一條測試。
    """
    return httpx.Client(base_url=base_url, transport=transport)


class _RerankClient:
    """兩家共用的規則：位址、憑證、逾時、錯誤分類、索引驗證。"""

    name = "rerank"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # 私有且不進 __repr__（見下）。provider 物件會整個被丟進 log 的情況很常見
        # ——設定 dump、例外的 locals、除錯時的 print。
        self._api_key = api_key
        self._transport = transport

    def __repr__(self) -> str:
        """**不含金鑰**（同 `_VendorClient`）。"""
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _post(self, path: str, payload: dict[str, Any], *, timeout_seconds: float) -> Any:
        """打一次，並把所有失敗收斂成 `ProviderError`。

        **回傳型別是 `Any` 而不是 dict**：TEI 回的是裸陣列、Jina 回的是物件，形狀的
        判斷屬於各自的 `rerank()`——在這裡先假設是 dict 的話，TEI 的正常回應會在這一層
        就被當成格式錯誤。
        """
        try:
            response = _shared_client(self._base_url, self._transport).post(
                path,
                json=payload,
                headers=self._headers(),
                # 11 §4：rerank 的預算是 1.2s，由 Gateway 傳進來（11 §4.1 的 timeout
                # 字典是全域的）。**逐次傳而不是綁在 client 上**：client 是共用的，
                # 而上限屬於這一次呼叫。
                timeout=httpx.Timeout(timeout_seconds),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} 逾時（{timeout_seconds:g}s）") from exc
        except httpx.HTTPError as exc:
            # 連不上、DNS、TLS。**TEI 沒開是開發機上最常見的一種**，而它必須降級成
            # 「跳過 rerank」而不是讓問答失敗——裸的 httpx 例外會穿過 service 的
            # `except ProviderError`。
            raise ProviderUnavailableError(f"{self.name} 連線失敗：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise _error_for(response, vendor=self.name)

        try:
            return response.json()
        except ValueError as exc:
            # 200 但不是 JSON：proxy 插了一頁 HTML、反向代理回了錯誤頁。
            raise ProviderUnavailableError(f"{self.name} 回應不是 JSON") from exc

    def _documents(self, rows: Any, *, count: int, score_key: str) -> list[RerankedDocument]:
        """provider 的排名 → `(原始索引, 分數)`，並驗到能安全地拿去對 chunk 為止。

        **這是正確性而不是防禦**：呼叫端拿 `index` 去索引自己的 `RetrievedChunk` 清單
        （`services/rag/retrieval.py`）。越界會是 `IndexError`（穿過降級處理，把一個
        可跳過的增強變成一次失敗的問答），而重複的索引會讓同一段 chunk 進 context 兩遍
        ——token 預算與引用編號跟著錯一位，且兩者都不會有任何錯誤訊息。

        **少回是合法的**（有些家會自己截斷），少的那幾筆就是沒進榜。
        """
        if not isinstance(rows, list):
            raise ProviderUnavailableError(f"{self.name} 的排名不是一個陣列")

        seen: set[int] = set()
        documents: list[RerankedDocument] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ProviderUnavailableError(f"{self.name} 的排名項目格式非預期")
            try:
                index = int(row["index"])
                score = float(row[score_key])
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderUnavailableError(f"{self.name} 的排名缺少必要欄位") from exc
            if not 0 <= index < count:
                raise ProviderUnavailableError(
                    f"{self.name} 回傳的索引 {index} 不在送出的 {count} 筆候選之內"
                )
            if index in seen:
                raise ProviderUnavailableError(f"{self.name} 重複回傳了索引 {index}")
            seen.add(index)
            documents.append(RerankedDocument(index=index, score=score))

        # **自己再排一次**：TEI 與 Jina 目前都回排好的，但那是它們的實作細節，而
        # Gateway 只做 `[:top_n]` 切片——順序錯的話，切掉的正是分數最高的那幾段。
        # 同分以索引為第二鍵（決定性，同 RRF 與 `MockRerankProvider`）：不決定的話，
        # 同一個問題兩次查詢可能給出不同的引用。
        documents.sort(key=lambda doc: (-doc.score, doc.index))
        return documents


class TeiRerankProvider(_RerankClient):
    """自架 HuggingFace TEI 的 `/rerank`（2B 的主線）。

    容器一次只服務一個模型（`bge-reranker-v2-m3`），所以請求裡沒有模型欄位，回應裡
    也沒有——`model` 因此照實回報我們設定的那個名字（06 §1 的版本化貫穿要留下「當時
    用了哪個模型」的快照，而這裡拿得到的最誠實的答案就是設定值）。
    """

    name = "tei"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url or TEI_DEFAULT_BASE_URL, transport=transport)

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        if not documents:
            # Gateway 已經擋了一層；這裡再擋是因為 adapter 也會被直接使用
            # （`make verify-provider`），而那一趟仍然要付延遲（TEI 要載入 batch）。
            return ProviderRerank(results=[], model=model)

        body = self._post(
            "/rerank",
            {
                "query": query,
                # 順序就是索引的定義：回來的 `index` 指的是這個陣列的第幾筆。
                "texts": list(documents),
                # **三個旗標全部明寫**，因為 TEI 的預設值全部不是我們要的：
                #
                # `raw_scores=true` 回的是 logits（可為負、無上界），而 06 §3.1 的絕對
                # 門檻 0.3 是 0~1 尺度上的數字——送錯這一個，門檻就從品質關卡變成隨機
                # 切割。
                "raw_scores": False,
                # 原文一個字都不需要（我們用索引對回自己的 chunk），而 24 段候選回傳
                # 原文是每次查詢多幾十 KB，全落在 1.2s 的預算裡。
                "return_text": False,
                # `bge-reranker-v2-m3` 的 context 是 512 token，而一個 chunk 加上標題就
                # 可能超過。不截斷的話，一段過長的候選會讓**整批** 413/422 ——而降級是
                # 靜默的，症狀只是「rerank 好像沒在動」。
                "truncate": True,
            },
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(body, list):
            raise ProviderUnavailableError(f"{self.name} 的回應不是排名陣列")

        return ProviderRerank(
            results=self._documents(body, count=len(documents), score_key="score"),
            model=model,
        )


class JinaRerankProvider(_RerankClient):
    """Jina 的 `/v1/rerank`（第二家）。

    存在的理由是**驗證 Gateway 沒綁死一家**，順帶讓沒有 GPU 的機器有東西可用。形狀與
    TEI 完全不同：欄位叫 `documents`、分數叫 `relevance_score`、回應是物件而且會回報
    實際使用的模型（雲端 API 會做別名解析，同 embedding 的 1C-5）。
    """

    name = "jina"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(base_url=base_url or JINA_BASE_URL, api_key=api_key, transport=transport)

    def rerank(
        self, query: str, documents: list[str], *, model: str, timeout_seconds: float
    ) -> ProviderRerank:
        if not documents:
            return ProviderRerank(results=[], model=model)

        body = self._post(
            "/rerank",
            {
                "model": model,
                "query": query,
                "documents": list(documents),
                # 同 TEI 的 `return_text`：我們只要索引。
                "return_documents": False,
            },
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(body, dict) or "results" not in body:
            raise ProviderUnavailableError(f"{self.name} 的回應沒有 results")

        return ProviderRerank(
            results=self._documents(
                body["results"], count=len(documents), score_key="relevance_score"
            ),
            # provider 回報的實際模型；沒回報就沿用請求值。
            model=str(body.get("model") or model),
        )


def _error_for(response: httpx.Response, *, vendor: str) -> ProviderError:
    """HTTP 狀態 → 我們的例外型別（形狀同 `openai_compatible._error_for`）。

    **Gateway 不重試 rerank**（2B-3），所以 `retryable` 在這條路上不決定行為——它決定
    的是**分類**：「TEI 過載所以這次跳過」與「金鑰過期所以永遠跳過」是兩件事，而混在
    一起的話，rerank 靜靜地停了三天不會有人看得出來（04 §「rerank 失敗不 raise」的
    代價就在這裡）。

    **訊息一律由我們自己組**，絕不回貼 provider 的原文（鐵則 9）：那些訊息常把整個
    請求（含 Authorization 標頭）回貼回來，而錯誤訊息會進 log 與 `usage.rag`。
    """
    status = response.status_code
    detail = {"status": status, "vendor": vendor}

    if status == 429:
        return ProviderRateLimitedError(f"{vendor} 頻率限制", details=detail)
    if status in (401, 403):
        return ProviderAuthError(f"{vendor} 拒絕了我們的憑證", details=detail)
    if status in (400, 413, 422):
        # 送出去的東西被退回來：批次太大、段落 tokenize 失敗、欄位名不對。重試三次
        # 回來的是同一個 413，而該改的是 `rag_hybrid_candidates` 或 chunk 大小。
        return ProviderRequestRejectedError(f"{vendor} 退回了這次請求", details=detail)
    # 424 是 TEI 的「推論失敗」（GPU OOM 最常見），5xx 是服務端的問題——下一次通常
    # 就好了。
    return ProviderUnavailableError(f"{vendor} 回應 {status}", details=detail)
