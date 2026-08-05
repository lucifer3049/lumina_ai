"""從 access log 算**伺服器端**延遲分位數 —— 單機壓測的校正工具。

locust 報的 p95 是**客戶端**量的：從發出請求到收到回應，中間包含 locust 自己
在同一台機器上等 CPU 的時間。個人開發沒有獨立負載機（11 §1.4 的已知偏離），
那段等待會被算進延遲，讓系統看起來比實際慢。

``api/main.py`` 的 access_log_middleware 記的 ``duration_ms`` 則是在**伺服器
行程內**量的，不含客戶端排隊。兩個數字對照就能分辨：

- 兩邊都慢 → 系統真的慢，調參有意義
- 客戶端慢、伺服器端不慢 → 慢的是量測工具本身，數字不能拿來調參

只讀 stdlib、不 import 專案任何一層（它是分析工具，不是應用程式碼）。

用法::

    make api-pinned            # 另一個視窗，log 會導到 $(API_LOG)
    make loadtest-pinned
    make loadtest-report
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ACCESS_EVENT = "http_request"


def _parse_ts(raw: object) -> datetime | None:
    """structlog 的 ``ts`` 欄位（ISO-8601，結尾 Z）轉 datetime；解不了回 None。"""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _durations(
    log_path: Path, *, path_filter: str | None, last_seconds: float | None
) -> list[float]:
    """取出符合條件的 ``duration_ms``。

    逐行解析而非整檔 ``json.loads``：檔案是 JSON Lines，而且壓測期間可能還在
    被寫入，最後一行常是半截的。壞行直接跳過——分析工具不該因為一行截斷就
    整個失敗，那會讓人以為壓測本身出事。

    ``last_seconds`` 只取「距離檔內最後一筆事件 N 秒內」的樣本。**API 不重啟而
    連跑多輪時這是必要的**：log 是持續附加的，不篩時間會把前幾輪一起算進去，
    第二輪之後的數字都是混合值（實測：run1 單獨 p95 295.9ms，run2 的累計報告
    變成 270.6ms——看起來變快，其實只是被前一輪稀釋）。
    另一個作用是排除暖機：第一輪含連線建立與 import 成本，比穩態慢一截。
    """
    parsed: list[tuple[datetime | None, float]] = []
    with log_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue  # uvicorn 啟動訊息等非 JSON 行
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != ACCESS_EVENT:
                continue
            if path_filter is not None and event.get("path") != path_filter:
                continue
            duration = event.get("duration_ms")
            if isinstance(duration, (int, float)):
                parsed.append((_parse_ts(event.get("ts")), float(duration)))

    if last_seconds is None:
        return [duration for _, duration in parsed]

    stamps = [ts for ts, _ in parsed if ts is not None]
    if not stamps:
        # 沒有可用時間戳就不要靜默地退回全量——那會讓人以為篩過了。
        print("log 內沒有可解析的 ts 欄位，--last-seconds 無法生效", file=sys.stderr)
        return [duration for _, duration in parsed]
    cutoff = max(stamps) - timedelta(seconds=last_seconds)
    return [duration for ts, duration in parsed if ts is not None and ts >= cutoff]


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """最近秩法（nearest-rank）取分位數。

    刻意不做內插：locust 報的也是近似分位數，兩邊用同一種定義才能直接對照。
    """
    if not sorted_values:
        return 0.0
    rank = max(1, min(len(sorted_values), round(fraction * len(sorted_values) + 0.5)))
    return sorted_values[rank - 1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("log", type=Path, help="access log（JSON Lines）路徑")
    parser.add_argument("--path", default=None, help="只算這個路徑，例如 /api/v1/spike/items")
    parser.add_argument(
        "--slo-ms",
        type=float,
        default=300.0,
        help="p95 目標值（11 §1.1「一般 API Response」預設 300ms）",
    )
    parser.add_argument(
        "--last-seconds",
        type=float,
        default=None,
        help="只算最後 N 秒的樣本（API 不重啟連跑多輪時必加，否則會混入前幾輪）",
    )
    args = parser.parse_args(argv)

    if not args.log.exists():
        print(f"找不到 {args.log}——請先用 `make api-pinned` 產生 log", file=sys.stderr)
        return 2

    values = _durations(args.log, path_filter=args.path, last_seconds=args.last_seconds)
    if not values:
        target = args.path or "(全部路徑)"
        print(f"{args.log} 裡沒有 {target} 的 {ACCESS_EVENT} 事件", file=sys.stderr)
        return 1

    values.sort()
    p95 = _percentile(values, 0.95)
    window = f"最後 {args.last_seconds:.0f} 秒" if args.last_seconds else "全部（含暖機）"
    print(f"來源     : {args.log}")
    print(f"路徑     : {args.path or '(全部)'}")
    print(f"時間窗   : {window}")
    print(f"樣本數   : {len(values)}")
    print(f"p50      : {_percentile(values, 0.50):.1f} ms")
    print(f"p95      : {p95:.1f} ms")
    print(f"p99      : {_percentile(values, 0.99):.1f} ms")
    print(f"max      : {values[-1]:.1f} ms")
    print(f"平均     : {statistics.fmean(values):.1f} ms")
    print(f"SLO      : p95 < {args.slo_ms:.0f} ms → {'PASS' if p95 < args.slo_ms else 'FAIL'}")
    # 退出碼刻意恆為 0：這是觀測工具，不是 CI 斷言。要當門檻用時由呼叫端判讀輸出，
    # 免得「基準線量測」在 SLO 未達時被誤當成腳本壞掉。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
