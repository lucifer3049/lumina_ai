"""驗收：`ChatService` 的兩條切縫（二次架構審計 F-07）。

第一輪審計的 F-07 是「ChatService 成為交會點」：814 行、建構子七個協作者、全 repo
唯一同時 import `ai/`、`rag/`、`platform/` 三個外部 context 的地方，而全 services/
的跨 context import 共 14 條、5 條在這一個檔案。時機的判斷是「現在不重構、**3A 前**
小幅切分」——因為 3A（Tool 系統）會把工具定義、可用性判斷與 schema 全部加進「組請求」
那一段，而那正是這個檔案最長的部分。

切出兩塊：`TurnBudget`（額度的預留與結算）與 `TurnComposer`（把請求組起來）。

**這一檔擋的是「切開之後又長回去」**。重構沒有測試會腐化得比新功能還快：沒有任何
東西阻止下一個人在 `chat.py` 裡直接 `import QuotaService` 再自己扣一次額度，而那
不會有任何症狀——它會通過所有既有測試。

刻意**不驗行數**：行數會隨註解密度浮動，而本 repo 的註解密度很高（那是刻意的）。
驗的是**依賴方向**與**職責歸屬**，那兩件事改了才是真的長回去。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from services.conversation import chat
from services.conversation.budget import TurnBudget
from services.conversation.composer import TurnComposer

_CHAT_SOURCE = Path(inspect.getfile(chat))


def _imported_names(path: Path) -> set[str]:
    """檔案 import 進來的模組路徑（`from a.b import c` → `a.b`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


class TestBudgetOwnsQuota:
    def test_chat_does_not_reserve_quota_itself(self) -> None:
        """`chat.py` 仍然 import `QuotaService`（型別註記與注入口需要），但**不得
        自己呼叫 `check_and_reserve`**——那是 `TurnBudget.reserve` 的職責，而它
        帶著「被擋時要把前面預留的全部還回去」這條規則。

        自己扣一次的話，被擋的請求會留下已扣掉的額度，而使用者端只看到 429。
        """
        source = _CHAT_SOURCE.read_text(encoding="utf-8")

        assert "check_and_reserve" not in source, (
            "chat.py 自己預留額度了——那條路徑的回滾規則在 TurnBudget 裡"
        )

    def test_the_budget_is_the_only_one_that_commits(self) -> None:
        """結算（commit／release）同理：三種資源三種處置，混在一起會出錯。"""
        source = _CHAT_SOURCE.read_text(encoding="utf-8")

        assert ".commit(" not in source
        assert "_quota.release" not in source

    def test_it_exposes_the_three_settlement_paths(self) -> None:
        """三種資源的結算方式不同，介面上要分得出來（見 `TurnBudget` 的 docstring）。"""
        for name in ("reserve", "release", "settle_tokens", "release_stream"):
            assert hasattr(TurnBudget, name), f"TurnBudget 少了 {name}"


class TestComposerOwnsTheRequest:
    def test_chat_no_longer_reaches_into_rag_internals(self) -> None:
        """組請求那一段整個搬走了，所以 `chat.py` 不該再認識 `rag.pipeline`
        （查詢改寫）與 `ai.prompts`（context 區塊的組法）。

        **這條是為 3A 準備的**：工具的 schema 也會長在那一層，留在這裡的話
        `chat.py` 會在 3A 再長幾百行。
        """
        imported = _imported_names(_CHAT_SOURCE)

        assert "rag.pipeline" not in imported
        assert "ai.prompts" not in imported

    def test_the_composer_does_not_know_how_a_turn_is_created(self) -> None:
        """`TurnComposer.compose` 收欄位而不是收 `TurnStarted`。

        那個型別是 `chat.py` 兩段之間的契約，帶著額度預留與 user_id，而組請求一個
        都用不到。反過來 import 會讓兩個模組互相依賴——而「組請求」不該認識
        「回合是怎麼建立的」。
        """
        imported = _imported_names(Path(inspect.getfile(TurnComposer)))

        assert "services.conversation.chat" not in imported

    def test_the_budget_does_not_know_about_the_composer(self) -> None:
        """兩塊之間也不該互相認識——它們在一次問答裡是前後段，不是上下層。"""
        imported = _imported_names(Path(inspect.getfile(TurnBudget)))

        assert not any(name.endswith("composer") for name in imported)


class TestTheInjectionSeamsSurvived:
    def test_existing_collaborators_can_still_be_injected(self) -> None:
        """**F-07 是重構不是改介面。** 既有測試注入的是 `prompts` / `retrieval` /
        `quota`，把注入口換掉會讓「這次改動有沒有改變行為」變得無從判斷。
        """
        parameters = inspect.signature(chat.ChatService.__init__).parameters

        for name in (
            "conversations",
            "messages",
            "prompts",
            "retrieval",
            "gateway",
            "usage",
            "quota",
        ):
            assert name in parameters, f"ChatService 的 {name} 注入口不見了"

    def test_the_new_pieces_are_injectable_too(self) -> None:
        """3A 要替換組請求那一段時，走的是這兩個口。"""
        parameters = inspect.signature(chat.ChatService.__init__).parameters

        assert "budget" in parameters
        assert "composer" in parameters
