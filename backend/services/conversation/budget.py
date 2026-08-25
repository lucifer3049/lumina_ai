"""TurnBudget —— 一次問答的額度預留與結算（04 §8.1、2A-2a；二次架構審計 F-07）。

**從 `ChatService` 切出來的第一塊。** 那個檔案在 2B 結束時是 814 行、建構子七個協作者，
而額度這條線的形狀與生成完全無關：它只認識 `QuotaService`，只在回合的**開頭與結尾**
各出現一次，中間那幾百行的串流迴圈碰不到它。切開之後 `ChatService` 少一個協作者，
而「額度在一次問答裡怎麼流動」變成一個可以單獨讀完的檔案。

**reserve / commit 兩段式的理由**（04 §8.1）：token 的實際用量要到生成**結束**才知道，
而擋線必須畫在**開始**之前。於是開場按估計值預留（併發之下不會集體衝過線），結束時
按 usage 事件的實際值校正。

**三種資源、三種結算方式**，混在一起會出錯：

- ``messages_day``：一送出就算數，不校正、不歸還（那則訊息確實存在了）。
- ``tokens_month``：預留 → 校正成實際值，**或整筆歸還**（provider 一個字都沒吐）。
- ``streams``：瞬時值，生成結束一定要還——不還的話第 N 輪之後這個租戶永遠 429，
  而那看起來像「配額壞了」而不是「洩漏」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from config.settings.app_settings import get_app_settings
from core.db import run_orm
from etl.tokens import estimate_tokens
from services.platform.quota import QuotaExceededError, QuotaReservation, QuotaService

__all__ = ["TurnBudget", "TurnReservations", "token_reserve_for"]


def token_reserve_for(question: str) -> int:
    """開場要預留多少 token 額度（reserve/commit 的 reserve 值）。

    **設定值 + 這個問題本身的估計量**。設定值（`quota_token_reserve_estimate`）涵蓋的
    是「答案 + context」那一段——它們的長度要到生成結束才知道，只能先估。但問題本身
    的長度**現在就知道**，而它可以差好幾個量級：一則貼滿的訊息（schema 上限 32,000
    字元）光是問題就三萬多 token，用一個固定的 2000 去擋線等於沒擋——真實用量要等
    收尾 commit 才追認，那時該擋的那幾則已經送出去了。

    高估的代價只有「月底最後幾則被提早擋下」，而低估的代價是超用之後才發現。
    """
    return get_app_settings().quota_token_reserve_estimate + estimate_tokens(question)


@dataclass(frozen=True, slots=True)
class TurnReservations:
    """一個回合預留到的東西。

    ``all`` 是給「整筆退回」用的（下游失敗時），另外兩個是給收尾時分別結算的——
    兩者的處置不同，所以分開拿而不是讓呼叫端去 `all` 裡面挑。
    """

    all: tuple[QuotaReservation, ...] = ()
    tokens: QuotaReservation | None = None
    stream: QuotaReservation | None = None


class TurnBudget:
    def __init__(self, *, quota: QuotaService | None = None) -> None:
        self._quota = quota or QuotaService()

    def reserve(self, tenant_id: uuid.UUID, *, question: str) -> TurnReservations:
        """三種額度一起預留；任何一關被擋就把前面的全部還回去並 raise。

        **自己清理而不是讓呼叫端 try**：被擋的請求**不得消耗額度**——第二關擋下時
        第一關已經扣掉了，不還的話一個永遠打不通的租戶每試一次就少一則訊息額度。
        呼叫端只需要處理「被擋了」這件事。

        `check_and_reserve` 對「不限制」的資源回 `None`（見 `QuotaService`），所以
        每一個都要判——直接 append 會把 `None` 放進歸還清單。
        """
        acquired: list[QuotaReservation] = []
        try:
            if messages := self._quota.check_and_reserve(tenant_id, "messages_day", 1):
                acquired.append(messages)
            tokens = self._quota.check_and_reserve(
                tenant_id, "tokens_month", token_reserve_for(question)
            )
            if tokens:
                acquired.append(tokens)
            stream = self._quota.check_and_reserve(tenant_id, "streams", 1)
            if stream:
                acquired.append(stream)
        except QuotaExceededError:
            self.release(TurnReservations(all=tuple(acquired)))
            raise
        return TurnReservations(all=tuple(acquired), tokens=tokens, stream=stream)

    def release(self, reservations: TurnReservations) -> None:
        """整筆退回（同步）。下游失敗時用——DB 那一步失敗不能吃掉額度。"""
        for reservation in reservations.all:
            self._quota.release(reservation)

    async def settle_tokens(
        self, reservation: QuotaReservation | None, usage: dict[str, Any]
    ) -> None:
        """token 額度的第二段：有實際用量就 commit 校正，沒有就整筆歸還。

        不歸還的話，provider 一個字都沒吐的失敗回合也吃掉 2000 的預留量——
        連續幾次失敗之後，額度被「失敗」吃光，而 usage_logs 裡一筆帳都沒有。
        """
        if reservation is None:
            return
        if usage:
            actual = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            await run_orm(self._quota.commit, reservation, actual=actual)
        else:
            await run_orm(self._quota.release, reservation)

    async def release_stream(self, reservation: QuotaReservation | None) -> None:
        """歸還並發位。**呼叫端要放在 finally**（見 `ChatService.generate`）：
        出口有完成、中止、error、例外四種，散寫漏掉哪一種那一種就開始洩漏。"""
        if reservation is None:
            return
        await run_orm(self._quota.release, reservation)
