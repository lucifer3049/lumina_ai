"""驗收：AI context 兩張表的 RLS（05 §5.1、13 §3 工作包 1D-3b）。

方法論同 `test_rls_knowledge.py` 與 `test_rls_conversation.py`，不重述：查詢一律繞過
Repository 直接下沒有 `WHERE tenant_id` 的原生 SQL（走 ORM 的話 filter 與 policy 會
同時生效，綠燈分不出是哪一道擋的），讀與寫分開驗（`USING` 管讀、`WITH CHECK` 管寫）。

**這一組有一個前面幾個 context 都沒有的形狀：`tenant_id IS NULL` 的系統模板。**
它與 `identity_role` 的系統角色同一種例外（見 `apps/identity/migrations/0002_rls.py`
的 docstring），而兩個方向的錯誤都很貴：

- 少寫 `tenant_id IS NULL OR ...`：系統模板對**所有**租戶隱形，每一次問答都找不到
  模板。這一條會當場爆，但爆在 1D-5，看起來像「問答壞了」。
- 寫成只比對 NULL 或條件寫反：一個租戶的客製模板會出現在別人的回答裡——那是把
  A 公司的內部指令（可能寫著他們的業務規則與禁區）餵給 B 公司的 LLM。

`prompt_versions` **沒有** `tenant_id` 欄位，它經 `prompt_id` 隸屬於 prompts。因此
它的 policy 是 `EXISTS (...)` 形式而不是直接比對——這一點必須有測試釘住，否則最省事
的寫法（不開 RLS）會讓「模板本體」完全不受保護，而 prompts 那張表只有標題與 key。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from django.db import connection, connections
from django.db.utils import ProgrammingError

from core.tenant import tenant_context
from core.uow import unit_of_work
from services.ai.prompts import SYSTEM_RAG_PROMPT_KEY
from tests.conftest import TENANT_A, TENANT_B
from tests.factories.ai import make_prompt, make_prompt_version
from tests.factories.identity import make_tenant, tenant_scope
from tests.seed import ensure_prompt_seed

# `admin` 也要列進來：系統模板的補種走 owner 連線（tests/seed.py 的
# `ensure_prompt_seed`——應用角色寫不出 `tenant_id IS NULL` 的列）。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

# **新表一律要加進這個清單**：漏掉的表不會有任何症狀——查詢照常回傳，只是範圍變成
# 整個資料庫（1C-2 的 embeddings 已經是同一個教訓）。
AI_TABLES = ("ai_prompt", "ai_promptversion")


def _raw_ids(table: str) -> set[uuid.UUID]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id FROM {table}")  # noqa: S608 —— 表名來自本檔常數
        return {row[0] for row in cursor.fetchall()}


@pytest.fixture
def prompts_in_both_tenants() -> Iterator[dict[str, uuid.UUID]]:
    """兩個租戶各一份自己的模板（各一個 published 版本）。系統模板由 migration 種下。"""
    # migration 種的系統模板會被前一條 transactional 測試 TRUNCATE 掉，補回來
    # （見 tests/seed.py）。本檔有一半的斷言就是在驗它對所有租戶可見。
    ensure_prompt_seed()
    created: dict[str, uuid.UUID] = {}
    for tenant_id, suffix in ((TENANT_A, "a"), (TENANT_B, "b")):
        with tenant_scope(tenant_id):
            make_tenant(id=tenant_id, slug=f"tenant-{suffix}")
            prompt = make_prompt(tenant_id=tenant_id, key=f"custom-{suffix}")
            version = make_prompt_version(
                prompt=prompt, version=1, status="published", template=f"{suffix} 的規則"
            )
        created[f"prompt_{suffix}"] = uuid.UUID(str(prompt.id))
        created[f"version_{suffix}"] = uuid.UUID(str(version.id))
    yield created


class TestPolicyIsEnabled:
    @pytest.mark.parametrize("table", AI_TABLES)
    def test_row_level_security_is_on_and_forced(self, table: str) -> None:
        """`FORCE` 不能少：migration 與維運腳本用的是 owner，而表的 owner 預設豁免
        policy（13 §3.1 的 1A-P1）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
                [table],
            )
            enabled, forced = cursor.fetchone()

        assert enabled and forced

    @pytest.mark.parametrize("table", AI_TABLES)
    def test_a_policy_exists(self, table: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_policies WHERE tablename = %s", [table])
            assert cursor.fetchone()[0] >= 1


class TestReadIsolation:
    def test_a_tenant_sees_only_its_own_prompts(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work():
            visible = _raw_ids("ai_prompt")

        assert prompts_in_both_tenants["prompt_a"] in visible
        assert prompts_in_both_tenants["prompt_b"] not in visible

    def test_versions_are_isolated_through_their_prompt(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """**模板本體在這張表**。prompts 擋住了而 versions 沒擋，等於門鎖了窗開著——
        而洩漏的正是內容最敏感的那一半。"""
        with tenant_context(TENANT_A), unit_of_work():
            visible = _raw_ids("ai_promptversion")

        assert prompts_in_both_tenants["version_a"] in visible
        assert prompts_in_both_tenants["version_b"] not in visible

    def test_the_system_template_is_visible_to_every_tenant(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """`tenant_id IS NULL` = 全租戶共用（同 identity 的系統角色）。

        看不到的話，每一次問答都會找不到模板——而錯誤會出現在 1D-5，看起來像問答壞了。
        """
        seen: dict[uuid.UUID, bool] = {}
        for tenant_id in (TENANT_A, TENANT_B):
            with tenant_context(tenant_id), unit_of_work(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM ai_prompt WHERE key = %s", [SYSTEM_RAG_PROMPT_KEY]
                )
                seen[tenant_id] = cursor.fetchone()[0] == 1

        assert seen[TENANT_A] and seen[TENANT_B]

    def test_the_system_templates_versions_are_visible_too(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """prompts 那張表放行、versions 沒放行的話，症狀是「找得到模板但讀不到內容」
        ——而那個查詢不會報錯，只會回 0 列。"""
        with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM ai_promptversion v "
                "JOIN ai_prompt p ON p.id = v.prompt_id WHERE p.key = %s",
                [SYSTEM_RAG_PROMPT_KEY],
            )
            assert cursor.fetchone()[0] >= 1

    def test_without_a_tenant_context_nothing_tenant_owned_is_visible(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """fail closed（1A-2 決定 A）：沒設租戶時看不到任何租戶的資料。

        系統模板仍看得見——它本來就不屬於任何租戶，而 `tenant_id IS NULL` 這個條件
        與 `app.tenant_id` 有沒有設無關。
        """
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ai_prompt WHERE tenant_id IS NOT NULL")
            assert cursor.fetchone()[0] == 0


class TestWriteIsolation:
    def test_writing_into_another_tenant_is_refused(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """只寫 `USING` 的 policy 擋得住讀、擋不住把資料寫進別人名下，而讀取測試永遠
        看不到這個漏洞。"""
        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            pytest.raises(ProgrammingError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO ai_prompt (id, tenant_id, key, name, description,"
                " created_at, updated_at) "
                "VALUES (%s, %s, 'x', 'x', '', now(), now())",
                [str(uuid.uuid4()), str(TENANT_B)],
            )

    def test_updating_another_tenants_prompt_touches_nothing(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """UPDATE 不會報錯——policy 讓那一列**不存在**，於是影響 0 列。

        這正是 RLS 最容易被誤讀的地方：沒有例外不代表寫成功了。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ai_prompt SET name = 'hijacked' WHERE id = %s",
                [str(prompts_in_both_tenants["prompt_b"])],
            )
            assert cursor.rowcount == 0

    def test_deleting_another_tenants_prompt_touches_nothing(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_prompt WHERE id = %s",
                [str(prompts_in_both_tenants["prompt_b"])],
            )
            assert cursor.rowcount == 0

    def test_a_tenant_cannot_forge_a_system_template(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """**一個租戶寫得出 `tenant_id IS NULL` 的列，就等於改得動所有人的 prompt。**

        identity 的系統角色 policy 對讀寫用的是同一個條件（那裡沒有寫入路徑，所以不
        成問題）；prompts 在 Phase 5 會有 `/prompts` 的寫入端點，因此這裡的 `WITH CHECK`
        必須比 `USING` 窄：讀得到 NULL，但只寫得進自己的 tenant_id。
        """
        with (
            tenant_context(TENANT_A),
            unit_of_work(),
            pytest.raises(ProgrammingError),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO ai_prompt (id, tenant_id, key, name, description,"
                " created_at, updated_at) "
                "VALUES (%s, NULL, 'forged', 'forged', '', now(), now())",
                [str(uuid.uuid4())],
            )

    def test_a_tenant_cannot_delete_the_system_template(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """0002 只擋住了「建立」。``FOR ALL`` 的 DELETE **只檢查 ``USING``**，而
        ``USING`` 為了讓所有租戶讀得到系統模板放行了 ``tenant_id IS NULL``。

        刪掉的後果比外洩更立即：`PromptVersion.prompt` 是 ``on_delete=CASCADE``，
        **FK 級聯不受 RLS 約束**，模板內容一起消失，所有租戶的問答同時失去依據。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM ai_prompt WHERE tenant_id IS NULL")
            assert cursor.rowcount == 0

        with connections["admin"].cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ai_prompt WHERE tenant_id IS NULL")
            assert cursor.fetchone()[0] >= 1

    def test_a_tenant_cannot_hijack_the_system_template(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """把系統模板的 ``tenant_id`` 改成自己：舊列過 ``USING``（NULL 放行）、新列過
        ``WITH CHECK``（是自己的租戶）——於是它變成某租戶私有，其他租戶的問答瞬間找不到
        模板，而資料庫裡沒有任何一列被刪。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ai_prompt SET tenant_id = %s WHERE tenant_id IS NULL", [str(TENANT_A)]
            )
            assert cursor.rowcount == 0

        with tenant_context(TENANT_B), unit_of_work(), connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM ai_prompt WHERE key = %s", [SYSTEM_RAG_PROMPT_KEY])
            assert cursor.fetchone()[0] == 1, "系統模板被劫持了——B 租戶已經看不到它"

    def test_a_tenant_cannot_delete_the_system_templates_versions(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """**模板本體在這張表**。prompts 收窄了而 versions 沒收，等於門鎖了、窗開著——
        而且刪掉的正是內容最敏感的那一半。

        版本表的 UPDATE 原本就擋住了（``WITH CHECK`` 要求父列屬於自己），DELETE 沒有
        ——它只看 ``USING``，而讀的條件必須放行系統模板的版本。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM ai_promptversion v USING ai_prompt p"
                " WHERE p.id = v.prompt_id AND p.tenant_id IS NULL"
            )
            assert cursor.rowcount == 0

        with connections["admin"].cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM ai_promptversion v JOIN ai_prompt p ON p.id = v.prompt_id"
                " WHERE p.tenant_id IS NULL"
            )
            assert cursor.fetchone()[0] >= 1

    def test_a_tenant_can_still_change_and_delete_its_own_prompt(
        self, prompts_in_both_tenants: dict[str, uuid.UUID]
    ) -> None:
        """收窄寫入方向的另一半：自己的模板必須照常改得動、刪得掉。

        只驗「擋住了」的話，``USING (false)`` 也會全綠——而那會讓 Phase 5 的 `/prompts`
        寫入端點整個失效，症狀是「存了沒反應也沒錯誤」（影響 0 列）。
        """
        with tenant_context(TENANT_A), unit_of_work(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE ai_prompt SET name = 'renamed' WHERE id = %s",
                [str(prompts_in_both_tenants["prompt_a"])],
            )
            assert cursor.rowcount == 1
            cursor.execute(
                "DELETE FROM ai_promptversion WHERE id = %s",
                [str(prompts_in_both_tenants["version_a"])],
            )
            assert cursor.rowcount == 1
