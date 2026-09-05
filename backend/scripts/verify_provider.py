"""手動驗證一家 provider 真的能用（1C-5）。

**這是 repo 內唯一會打真 API 的東西**，而且只在人手動執行時。自動測試一律用假的
HTTP 層（CLAUDE.md），那驗得了「我們送出去的請求長什麼樣」，驗不了「那家真的收不收」
——base_url 少一個字、認證標頭格式不對、Gemini 的相容端點吃不吃 `dimensions`，這幾類
都要真的打一次才知道。

接進 CI 的代價很具體：CI 會開始花錢，而且會因為別人的服務中斷而紅——那種紅燈與這次
改動無關，久了就沒有人看紅燈了。`tests/unit/test_dev_launcher.py::TestProviderVerification`
守著這條界線。

用法：

    make verify-provider PROVIDER=gemini                    # embedding（預設）
    make verify-provider PROVIDER=gemini CAPABILITY=chat    # 串流對話（1D-3a）
    make verify-provider PROVIDER=tei CAPABILITY=rerank     # 本機 TEI（2B-4；先 make tei-up）
    make verify-provider PROVIDER=jina CAPABILITY=rerank    # 雲端 rerank（需金鑰）

金鑰取自 `AI_EMBEDDING_API_KEY` / `AI_CHAT_API_KEY`（vLLM／TEI 不需要）。它只印出維度、
用量與耗時，**不印金鑰、也不印向量內容**。

串流那一條要驗的東西與 embedding 不同，而且更驗不出來：那家有沒有照 SSE 的格式送、
`[DONE]` 是不是真的會到、`stream_options.include_usage` 那家認不認得（不認得的話每一次
對話的成本都只能用估的）。這些在假的 HTTP 層之下全部是我們自己寫的預期值。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import cast

# 直接執行時 sys.path[0] 是 scripts/，`import ai.gateway` 會失敗（pyproject 的
# `pythonpath = ["."]` 只對 pytest 生效）。同 scripts/export_openapi.py。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 這支腳本在 Django 之外執行（它不碰 DB），但 AppSettings 要讀 .env。
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from ai.gateway.providers import RerankProvider  # noqa: E402
from ai.gateway.providers.openai_compatible import (  # noqa: E402
    VENDORS,
    OpenAICompatibleProvider,
)
from core.exceptions import ProviderError  # noqa: E402

# rerank 的兩家不在 `VENDORS` 裡——那張表是 OpenAI 相容端點的清單，而 rerank 沒有
# 共通形狀（13 §4 的定案，見 ai/gateway/providers/rerank.py）。
RERANK_PROVIDERS = ("tei", "jina")

_SAMPLES = [
    "員工請假應於三日前提出申請。",
    "Annual performance reviews take place every December.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="打一次真的 API，確認 adapter 能用")
    parser.add_argument("--provider", required=True, choices=sorted({*VENDORS, *RERANK_PROVIDERS}))
    parser.add_argument(
        "--capability", default="embedding", choices=("embedding", "chat", "rerank")
    )
    parser.add_argument("--model", default=None, help="預設取 AI_EMBEDDING_MODEL／AI_CHAT_MODEL")
    parser.add_argument("--dimensions", type=int, default=None, help="預設取設定值（embedding）")
    parser.add_argument(
        "--reasoning",
        default="off",
        choices=("off", "low", "medium", "high"),
        help="chat：試送 reasoning_effort，用來決定 VendorSpec 的旗標能不能打開",
    )
    parser.add_argument(
        "--json", action="store_true", help="chat：試送 response_format=json_object"
    )
    args = parser.parse_args()

    if args.capability == "rerank":
        if args.provider not in RERANK_PROVIDERS:
            print(
                f"✗ {args.provider} 不是 rerank provider（可用：{', '.join(RERANK_PROVIDERS)}）",
                file=sys.stderr,
            )
            return 2
        return _verify_rerank(vendor=args.provider, model=args.model)

    if args.provider not in VENDORS:
        print(f"✗ {args.provider} 只支援 CAPABILITY=rerank", file=sys.stderr)
        return 2

    if args.capability == "chat":
        return asyncio.run(
            _verify_chat(
                vendor=args.provider,
                model=args.model,
                reasoning=args.reasoning,
                json_mode=args.json,
            )
        )

    from config.settings.app_settings import get_app_settings

    settings = get_app_settings()
    spec = VENDORS[args.provider]
    key = settings.ai_embedding_api_key
    if spec.requires_api_key and key is None:
        print(f"✗ {args.provider} 需要金鑰：請設定 AI_EMBEDDING_API_KEY", file=sys.stderr)
        return 2

    model = args.model or settings.ai_embedding_model
    dimensions = args.dimensions or settings.ai_embedding_dimensions
    provider = OpenAICompatibleProvider(
        vendor=args.provider,
        api_key=key.get_secret_value() if key else None,
        dimensions=dimensions,
        base_url=settings.ai_embedding_base_url or None,
    )

    print(f"provider={args.provider}  model={model}  要求維度={dimensions}")
    started = time.monotonic()
    try:
        result = provider.embed(
            _SAMPLES, model=model, timeout_seconds=settings.ai_embedding_timeout_seconds
        )
    except ProviderError as exc:
        # 只印我們自己的訊息——provider 的原文可能夾著金鑰（見 adapter 的 _error_for）。
        print(f"✗ {exc}  (retryable={exc.retryable})", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    actual = len(result.vectors[0])
    print(f"  回報模型={result.model}")
    print(f"  實際維度={actual}")
    print(f"  用量 tokens={result.prompt_tokens}")
    print(f"  耗時={elapsed:.2f}s")

    if actual != dimensions:
        # 這正是這支腳本最主要的用途：確認那家真的吃 `dimensions`。不吃的話，
        # embedding 會在寫入時被 DB 擋下，而錯誤指向 INSERT。
        hint = (
            "那家不支援 dimensions 參數"
            if not spec.supports_dimensions
            else "相容端點可能忽略了這個參數"
        )
        print(f"✗ 維度不符：要求 {dimensions}，拿到 {actual}。{hint}", file=sys.stderr)
        return 1

    norm = sum(value * value for value in result.vectors[0]) ** 0.5
    print(f"  單位長度={norm:.6f}（應為 1.0）")
    print("✓ 通過")
    return 0


def _verify_rerank(*, vendor: str, model: str | None) -> int:
    """打一次真的 rerank（2B-4）。**這支腳本是 11 §4「rerank < 800ms」的量測工具。**

    驗三件事，而三件事在假的 HTTP 層之下全部是我們自己寫的預期值：

    1. **分數尺度真的是 0~1**。06 §3.1 的絕對門檻 0.3 靠它。TEI 的 `raw_scores` 旗標
       送錯就會拿到 logits，而排序看起來完全正常——只有門檻會安靜地砍錯東西。
    2. **模型真的是多語的**（06 §3.4 的硬性條件）。題目是中文、正解是英文段落：單語
       reranker 會把它打低分，比不 rerank 更糟。這是這支腳本最主要的用途。
    3. **耗時**。1.2s 逾時即跳過（11 §4），而那個預算是照著本機 TEI 訂的——換一台
       機器、換一個 batch 大小都要重量一次。
    """
    from config.settings.app_settings import get_app_settings

    settings = get_app_settings()
    rerank_model = model or settings.ai_rerank_model
    base_url = settings.ai_rerank_base_url or None

    provider: RerankProvider
    if vendor == "tei":
        from ai.gateway.providers.rerank import TeiRerankProvider

        provider = TeiRerankProvider(base_url=base_url)
    else:
        from ai.gateway.providers.rerank import JinaRerankProvider

        key = settings.ai_rerank_api_key
        if not (key and key.get_secret_value()):
            print(f"✗ {vendor} 需要金鑰：請設定 AI_RERANK_API_KEY", file=sys.stderr)
            return 2
        provider = JinaRerankProvider(api_key=key.get_secret_value(), base_url=base_url)

    query = "員工請假要提前幾天申請？"
    # 索引 2 是正解，而且**是英文的**——跨語言那一條就是這樣驗的。
    documents = [
        "年度考核於每年十二月進行，由直屬主管填寫評核表。",
        "出差旅費以實報實銷為原則，需檢附發票正本。",
        "Employees must submit leave requests at least three days in advance.",
        "公司午餐時間為十二點至十三點。",
    ]
    expected = 2

    print(f"provider={vendor}  model={rerank_model}  候選={len(documents)} 段")
    started = time.monotonic()
    try:
        result = provider.rerank(
            query,
            documents,
            model=rerank_model,
            timeout_seconds=max(settings.ai_rerank_timeout_seconds, 30.0),
        )
    except ProviderError as exc:
        # 只印我們自己的訊息——provider 的原文可能夾著金鑰。
        print(f"✗ {exc}  (retryable={exc.retryable})", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(f"  回報模型={result.model}")
    print(f"  耗時={elapsed * 1000:.0f}ms（11 §4 的預算：< 800ms，逾 1.2s 跳過）")
    for rank, document in enumerate(result.results, start=1):
        marker = " ←正解" if document.index == expected else ""
        print(f"  #{rank} index={document.index} score={document.score:.4f}{marker}")

    failures = []
    if not all(0.0 <= document.score <= 1.0 for document in result.results):
        failures.append("分數不在 0~1（TEI 的 raw_scores 送錯了？絕對門檻會失效）")
    if not result.results or result.results[0].index != expected:
        failures.append("跨語言的正解沒有排第一——這個模型可能不是多語的（06 §3.4）")
    # **逾時只警告不失敗**：第一次呼叫含模型載入與 CUDA graph 暖機，那不是穩態延遲。
    if elapsed > 0.8:
        print("  ⚠ 超過 11 §4 的 800ms 預算（首次呼叫含暖機，請再跑一次確認）")

    if failures:
        for failure in failures:
            print(f"✗ {failure}", file=sys.stderr)
        return 1

    print("✓ 通過")
    return 0


async def _verify_chat(
    *, vendor: str, model: str | None, reasoning: str = "off", json_mode: bool = False
) -> int:
    """打一次真的串流生成（1D-3a）。

    印**首 token 延遲**而不只是總耗時：11 §1 的 latency budget 管的是 TTFT，而它與
    總時長沒有固定比例——一個 TTFT 很久但吐得很快的模型，總耗時看起來完全正常。

    `--reasoning` / `--json` 是**用來蒐證的**：`VendorSpec` 的那兩個旗標預設 False
    （見那裡的說明），要打開得先在這裡對那一家實測通過。所以這兩個旗標刻意**繞過**
    adapter 的降級判斷直接送出去——降級判斷正是這支腳本要驗的對象。
    """
    from ai.gateway.chat import (
        ChatMessage,
        ChatRequest,
        ChatTimeouts,
        ReasoningEffort,
        TextDelta,
        UsageDelta,
    )
    from ai.gateway.providers.openai_compatible import OpenAICompatibleChatProvider
    from config.settings.app_settings import get_app_settings

    settings = get_app_settings()
    spec = VENDORS[vendor]
    key = settings.ai_chat_api_key
    if spec.requires_api_key and not (key and key.get_secret_value()):
        print(f"✗ {vendor} 需要金鑰：請設定 AI_CHAT_API_KEY", file=sys.stderr)
        return 2

    chat_model = model or settings.ai_chat_model
    provider = OpenAICompatibleChatProvider(
        vendor=vendor,
        api_key=key.get_secret_value() if key else None,
        base_url=settings.ai_chat_base_url or None,
    )
    if reasoning != "off" or json_mode:
        # **刻意打開旗標再送**：adapter 的降級判斷（那家沒實測過就不送）正是這裡要
        # 驗的對象，照它的判斷走就永遠送不出去，也就永遠拿不到可以改旗標的證據。
        # 這是診斷工具的權限，不是 production 路徑——因此覆寫發生在腳本裡，而不是
        # 在 adapter 上開一個只有腳本會用的參數。
        provider._spec = dataclasses.replace(
            VENDORS[vendor], supports_reasoning_effort=True, supports_response_format=True
        )

    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content="用一句話回答，不要解釋。"),
            ChatMessage(role="user", content="台灣的首都是哪裡？"),
        ],
        model=chat_model,
        # argparse 的 `choices` 已經把值限定成那四個，但型別上仍是 str。
        reasoning_effort=cast("ReasoningEffort", reasoning),
        response_format={"type": "json_object"} if json_mode else None,
    )

    print(f"provider={vendor}  model={chat_model}  reasoning={reasoning}  json={json_mode}")
    started = time.monotonic()
    first_token_at: float | None = None
    text: list[str] = []
    usage: UsageDelta | None = None
    try:
        async for delta in provider.stream_chat(request, timeouts=ChatTimeouts.from_settings()):
            if isinstance(delta, TextDelta):
                if first_token_at is None:
                    first_token_at = time.monotonic()
                text.append(delta.text)
            elif isinstance(delta, UsageDelta):
                usage = delta
    except ProviderError as exc:
        # 同 embedding：只印我們自己的訊息，provider 的原文可能夾著金鑰。
        print(f"✗ {exc}  (retryable={exc.retryable})", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    answer = "".join(text)
    ttft = f"{first_token_at - started:.2f}s" if first_token_at else "（沒有收到任何 token）"
    print(f"  首 token={ttft}")
    print(f"  總耗時={elapsed:.2f}s")
    print(f"  回答長度={len(answer)} 字元")
    print(f"  回答={answer[:120]}")

    if not answer:
        print("✗ 一個 token 都沒收到", file=sys.stderr)
        return 1
    if usage is None:
        # 不是致命錯誤，但要說出來：那家不認得 `stream_options.include_usage`，於是
        # 這條路徑上的成本統計會全部退回估算值（2A 對帳時會對不上真帳單）。
        print("⚠ 沒有 usage：那家不吃 stream_options.include_usage，成本只能估")
    else:
        print(f"  用量 prompt={usage.prompt_tokens} output={usage.billable_output_tokens}")

    if reasoning != "off" or json_mode:
        # 走到這裡代表那家收下了那些參數（不收的話上面早就以 400 失敗了）。
        flags = []
        if reasoning != "off":
            flags.append("supports_reasoning_effort=True")
        if json_mode:
            flags.append("supports_response_format=True")
        print(f"  → 可將 VENDORS[{vendor!r}] 的 {'、'.join(flags)} 打開（本次實測通過）")
    print("✓ 通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
