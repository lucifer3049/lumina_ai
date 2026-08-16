"""手動驗證一家 provider 真的能用（1C-5）。

**這是 repo 內唯一會打真 API 的東西**，而且只在人手動執行時。自動測試一律用假的
HTTP 層（CLAUDE.md），那驗得了「我們送出去的請求長什麼樣」，驗不了「那家真的收不收」
——base_url 少一個字、認證標頭格式不對、Gemini 的相容端點吃不吃 `dimensions`，這幾類
都要真的打一次才知道。

接進 CI 的代價很具體：CI 會開始花錢，而且會因為別人的服務中斷而紅——那種紅燈與這次
改動無關，久了就沒有人看紅燈了。`tests/unit/test_dev_launcher.py::TestProviderVerification`
守著這條界線。

用法：

    make verify-provider PROVIDER=gemini

金鑰取自 `AI_EMBEDDING_API_KEY`（Ollama 不需要）。它只印出維度、用量與耗時，
**不印金鑰、也不印向量內容**。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 直接執行時 sys.path[0] 是 scripts/，`import ai.gateway` 會失敗（pyproject 的
# `pythonpath = ["."]` 只對 pytest 生效）。同 scripts/export_openapi.py。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 這支腳本在 Django 之外執行（它不碰 DB），但 AppSettings 要讀 .env。
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

from ai.gateway.providers.openai_compatible import VENDORS, OpenAICompatibleProvider  # noqa: E402
from core.exceptions import ProviderError  # noqa: E402

_SAMPLES = [
    "員工請假應於三日前提出申請。",
    "Annual performance reviews take place every December.",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="打一次真的 embedding API，確認 adapter 能用")
    parser.add_argument("--provider", required=True, choices=sorted(VENDORS))
    parser.add_argument("--model", default=None, help="預設取 AI_EMBEDDING_MODEL")
    parser.add_argument("--dimensions", type=int, default=None, help="預設取設定值")
    args = parser.parse_args()

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


if __name__ == "__main__":
    raise SystemExit(main())
