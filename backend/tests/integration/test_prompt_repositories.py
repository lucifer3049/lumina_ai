"""驗收：Prompt 的資料層與版本解析（05 §3.3、04 §5.3、13 §3 工作包 1D-3b）。

13 對 1D 的範圍寫的是「Prompt Builder（版本機制**簡化版**：僅 draft/published）」。
簡化掉的是 `archived` 與發佈流程的 UI（`/prompts` 端點屬 09 §2.5，Phase 1 不做），
**沒有簡化掉的是不可變性**——那是版本機制存在的理由本身。

三件事錯了都不會有例外：

1. **published 的模板被改掉**。05 §3.3 明訂「published 後不可變（DB trigger 拒絕
   UPDATE template）」。可變的話，`messages.prompt_version`（05 §3.4）記的那個版本號
   就不再指向一份確定的內容——「這個回答當時用了什麼指令」永遠答不出來，而那正是
   06 §1 的「版本化貫穿」要保證的事。
2. **draft 被拿去服務線上流量**。改到一半的模板會直接影響所有人的回答，而它看起來
   只是「今天的答案怪怪的」。
3. **系統模板對租戶隱形**。`tenant_id IS NULL` 是全租戶共用（同 identity 的系統角色，
   見 `apps/identity/migrations/0002_rls.py`）；照一般 RLS 形狀寫的話，NULL 與任何值
   比較都是 NULL 而非 true，於是**每一次問答都會找不到模板**。這一條是三者中唯一
   會當場爆的，但爆的位置在 1D-5，看起來像「問答壞了」而不是「RLS 寫錯了」。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.utils import IntegrityError

from core.exceptions import NotFoundError
from core.tenant import tenant_context
from core.uow import unit_of_work
from repositories.ai import PromptRepository
from services.ai.prompts import SYSTEM_RAG_PROMPT_KEY, PromptService
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.ai import make_prompt, make_prompt_version
from tests.factories.identity import make_tenant, tenant_scope
from tests.seed import ensure_prompt_seed

# `admin` 也要列進來：系統模板的補種走 owner 連線（tests/seed.py 的
# `ensure_prompt_seed`——應用角色寫不出 `tenant_id IS NULL` 的列）。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])


@pytest.fixture
def tenants() -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    # `transaction=True` 的測試會 TRUNCATE 掉 migration 種的系統模板（同 identity 的
    # 系統角色）。補回來的內容直接取自那個 migration 模組，見 tests/seed.py。
    ensure_prompt_seed()
    for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{suffix}")
    yield TENANT_A, TENANT_B


class TestSeededSystemTemplate:
    """migration 種下的 RAG 主模板——1D-5 的問答直接站在它上面。"""

    def test_it_exists_and_is_published(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            version = PromptRepository().active_version(SYSTEM_RAG_PROMPT_KEY)

        assert version is not None
        assert version.status == "published"

    def test_it_is_a_system_template(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """`tenant_id` 是 NULL（05 §3.3）：一份模板服務所有租戶。

        每個租戶各種一份的話，改一次系統模板要回填全部租戶，而漏掉的那些會停在舊
        版本——它們的回答會與其他人不同，且沒有任何地方看得出來。
        """
        with tenant_context(TENANT_A), unit_of_work():
            prompt = PromptRepository().by_key(SYSTEM_RAG_PROMPT_KEY)

        assert prompt is not None
        assert prompt.tenant_id is None

    @pytest.mark.parametrize(
        ("rule", "why"),
        [
            ("context", "沒有「只依據 context 回答」就沒有 RAG，只有一個會查資料的聊天機器人"),
            ("[c:", "引用標記是 06 §3.3 的第二道 hallucination 防線與引用面板的原料"),
            ("不知道", "無據時要能誠實說不知道——硬答是這個產品最嚴重的失效"),
            ("語言", "06 §3.4：回答語言跟隨提問語言，與文件語言無關"),
        ],
    )
    def test_it_states_the_four_generation_rules(
        self, rule: str, why: str, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """06 §3.1 的 Generation 規則要**真的寫在模板裡**。

        釘住它是因為模板是純文字：少寫一條不會有任何測試變紅，而症狀分別是「開始
        自由發揮」「沒有引用」「不會說不知道」「用英文回答中文問題」——四種都會被
        當成「模型不夠好」。
        """
        with tenant_context(TENANT_A), unit_of_work():
            version = PromptRepository().active_version(SYSTEM_RAG_PROMPT_KEY)

        assert version is not None
        assert rule in version.template, why

    def test_it_carries_no_secrets(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """10 §5 的設計原則：**system prompt 不含 secrets**，所以洩漏它是尷尬而不是
        資安事件。這條測試把那個原則變成可驗證的東西。"""
        with tenant_context(TENANT_A), unit_of_work():
            version = PromptRepository().active_version(SYSTEM_RAG_PROMPT_KEY)

        assert version is not None
        lowered = version.template.lower()
        for smell in ("api_key", "password", "secret", "bearer ", "postgres://"):
            assert smell not in lowered


class TestVersionResolution:
    def test_draft_is_never_served(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """草稿是「改到一半」。被選中的話，一個還沒 review 的指令會直接影響所有回答。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="tenant-only")
            make_prompt_version(prompt=prompt, version=1, status="draft", template="草稿")

        with tenant_context(TENANT_A), unit_of_work():
            assert PromptRepository().active_version("tenant-only") is None

    def test_the_active_version_wins_over_a_newer_draft(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """「最新」不等於「生效中」：v2 還在草稿時，服務的仍是 v1。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="k")
            published = make_prompt_version(
                prompt=prompt, version=1, status="published", template="第一版"
            )
            make_prompt_version(prompt=prompt, version=2, status="draft", template="第二版")
            prompt.active_version_id = published.id
            with unit_of_work():
                prompt.save(update_fields=["active_version_id"])

        with tenant_context(TENANT_A), unit_of_work():
            version = PromptRepository().active_version("k")

        assert version is not None
        assert version.version == 1

    def test_a_tenant_template_overrides_the_system_one(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """同一個 key 的租戶模板優先（05 §3.3 的 `UNIQUE(tenant_id, key)` 就是為它留的）。

        反過來的話，租戶客製完全不生效，而畫面上那份模板看起來好好的。
        """
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key=SYSTEM_RAG_PROMPT_KEY)
            own = make_prompt_version(
                prompt=prompt, version=1, status="published", template="本租戶自己的規則"
            )
            prompt.active_version_id = own.id
            with unit_of_work():
                prompt.save(update_fields=["active_version_id"])

        with tenant_context(TENANT_A), unit_of_work():
            mine = PromptRepository().active_version(SYSTEM_RAG_PROMPT_KEY)
        with tenant_context(TENANT_B), unit_of_work():
            theirs = PromptRepository().active_version(SYSTEM_RAG_PROMPT_KEY)

        assert mine is not None and mine.template == "本租戶自己的規則"
        assert theirs is not None and theirs.template != "本租戶自己的規則", (
            "B 租戶不該看到 A 的客製模板"
        )


class TestImmutability:
    """05 §3.3：**published 後不可變**，由 DB trigger 拒絕。

    為什麼是 trigger 而不是 service 的判斷：`messages.prompt_version` 是回答的快照，
    而「當時用了哪一版」必須永遠指得回同一份內容。Service 那一層擋得住我們自己寫的
    程式碼，擋不住 Django Admin（2C 明訂用它頂替管理面）、`manage.py shell` 與手動
    的維運 SQL——而那三條路徑正是「臨時改一下線上 prompt」最常發生的地方。
    """

    def test_updating_a_published_template_is_refused(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="frozen")
            version = make_prompt_version(
                prompt=prompt, version=1, status="published", template="原文"
            )

        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            # trigger 用 ERRCODE `restrict_violation`（23001），psycopg 與 Django 把
            # 那一類對映成 IntegrityError——語意正確：這是「違反了一條約束」，
            # 而不是程式寫錯（ProgrammingError）或伺服器內部錯誤。
            pytest.raises(IntegrityError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE ai_promptversion SET template = %s WHERE id = %s",
                ["偷改的內容", str(version.id)],
            )

    def test_a_draft_is_still_editable(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """不可變只適用於已發佈的版本——草稿不能改的話，這個機制就沒辦法用了。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="editable")
            version = make_prompt_version(
                prompt=prompt, version=1, status="draft", template="第一稿"
            )
            version.template = "第二稿"
            with unit_of_work():
                version.save(update_fields=["template"])

        with tenant_context(TENANT_A), unit_of_work():
            reloaded = PromptRepository().version_by_id(version.id)

        assert reloaded is not None and reloaded.template == "第二稿"

    def test_publishing_a_draft_is_allowed(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """draft → published 是**狀態**的變更，不是內容的變更。trigger 若把它一起擋掉，
        就沒有任何版本能被發佈（而那會在第一次發佈時才發現）。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="publishable")
            version = make_prompt_version(prompt=prompt, version=1, status="draft", template="內容")
            version.status = "published"
            with unit_of_work():
                version.save(update_fields=["status"])

        with tenant_context(TENANT_A), unit_of_work():
            reloaded = PromptRepository().version_by_id(version.id)

        assert reloaded is not None and reloaded.status == "published"

    def test_a_published_version_cannot_be_reverted_to_draft(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """**兩步繞過**：先把 status 改回 draft（內容沒動，0003 的 trigger 放行），再改
        template（此時 ``OLD.status`` 已是 draft，照樣放行）。

        這個 trigger 存在的理由正是擋 Django Admin、``manage.py shell`` 與手動維運 SQL
        ——而那三條路徑做兩次 UPDATE 毫無障礙。所以「不可變」必須連狀態的方向一起擋
        （0006_published_status_guard）。
        """
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="no-revert")
            version = make_prompt_version(
                prompt=prompt, version=1, status="published", template="原文"
            )

        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            pytest.raises(IntegrityError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE ai_promptversion SET status = 'draft' WHERE id = %s", [str(version.id)]
            )

        with tenant_context(TENANT_A), unit_of_work():
            reloaded = PromptRepository().version_by_id(version.id)

        assert reloaded is not None
        assert reloaded.status == "published"
        assert reloaded.template == "原文"

    def test_archiving_a_published_version_is_allowed(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """``archived`` 是 Phase 5 發佈流程淘汰舊版本的路徑，擋掉它等於沒有版本汰換。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="archivable")
            version = make_prompt_version(
                prompt=prompt, version=1, status="published", template="內容"
            )
            version.status = "archived"
            with unit_of_work():
                version.save(update_fields=["status"])

        with tenant_context(TENANT_A), unit_of_work():
            reloaded = PromptRepository().version_by_id(version.id)

        assert reloaded is not None and reloaded.status == "archived"

    def test_an_archived_version_is_frozen_too(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """只擋「退回 draft」的話，``published → archived → 改內容`` 是同一個繞過換了個
        中繼站——而 archived 正是歷史回答指過去最多的那一批版本。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="frozen-archive")
            version = make_prompt_version(
                prompt=prompt, version=1, status="published", template="原文"
            )
            version.status = "archived"
            with unit_of_work():
                version.save(update_fields=["status"])

        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            pytest.raises(IntegrityError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE ai_promptversion SET template = %s WHERE id = %s",
                ["偷改的內容", str(version.id)],
            )


class TestUniqueness:
    def test_one_key_per_tenant(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """`UNIQUE(tenant_id, key)`（05 §3.3）。兩份同 key 的模板存在時，「用哪一份」
        取決於查詢順序——而那會隨資料量與 planner 改變。"""
        with tenant_scope(TENANT_A):
            make_prompt(tenant_id=TENANT_A, key="dup")

        with pytest.raises(IntegrityError), tenant_scope(TENANT_A):
            make_prompt(tenant_id=TENANT_A, key="dup")

    def test_the_same_key_in_another_tenant_is_fine(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """key 是租戶內唯一，不是全域唯一——否則第二個租戶就不能有自己的客製模板。"""
        for tenant_id in (TENANT_A, TENANT_B):
            with tenant_scope(tenant_id):
                make_prompt(tenant_id=tenant_id, key="same-key")

    def test_one_version_number_per_prompt(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """`UNIQUE(prompt_id, version)`（05 §7）：版本號是回答快照指過來的鍵。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="versioned")
            make_prompt_version(prompt=prompt, version=1, status="draft", template="a")

        with pytest.raises(IntegrityError), tenant_scope(TENANT_A):
            make_prompt_version(prompt=prompt, version=1, status="draft", template="b")


class TestPromptService:
    """04 §5.3 的介面：`render(prompt_key, version, variables) -> RenderedPrompt`。"""

    def test_render_returns_the_active_version_number(
        self, tenants: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """版本號要跟著渲染結果一起回來——1D-5 要把它寫進 `messages.prompt_version`
        （05 §3.4 的生成快照）。回不出來的話，那個欄位只能填 NULL，而「當時用了哪一版」
        就永遠答不出來了。"""
        rendered = PromptService().render(TENANT_A, key=SYSTEM_RAG_PROMPT_KEY)

        assert rendered.version >= 1
        assert rendered.key == SYSTEM_RAG_PROMPT_KEY

    def test_render_produces_the_system_text(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        rendered = PromptService().render(TENANT_A, key=SYSTEM_RAG_PROMPT_KEY)

        assert rendered.system.strip()
        assert "{{" not in rendered.system

    def test_an_unknown_key_is_not_found(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """**不能退回一個內建的預設字串**：那會讓「模板沒種好」變成一個安靜的降級，
        而系統會用一份沒有人 review 過的指令回答所有問題。"""
        with pytest.raises(NotFoundError):
            PromptService().render(TENANT_A, key="does-not-exist")

    def test_a_specific_version_can_be_pinned(self, tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
        """指定版本是 3B 評測的前提（跑同一題比較 v1 與 v2），也是事故回溯的入口。"""
        with tenant_scope(TENANT_A):
            prompt = make_prompt(tenant_id=TENANT_A, key="pinned")
            first = make_prompt_version(
                prompt=prompt, version=1, status="published", template="第一版"
            )
            second = make_prompt_version(
                prompt=prompt, version=2, status="published", template="第二版"
            )
            prompt.active_version_id = second.id
            with unit_of_work():
                prompt.save(update_fields=["active_version_id"])
        assert first is not None

        rendered = PromptService().render(TENANT_A, key="pinned", version=1)

        assert rendered.version == 1
        assert "第一版" in rendered.system
