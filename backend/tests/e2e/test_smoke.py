"""E2E smoke suite（13 §1.2）：登入 → 上傳 → ready → 問答 → 引用，5 分鐘內跑完。

每次任務結束必跑（``make smoke``）；不過 = 任務未完成。目的不是覆蓋率，是
「跨 session 開發時，上一輪還活著的價值迴路這一輪也還活著」。

骨架階段（1A-5）只有第 1 步是活的；第 2–5 步是佔位，工作包做到哪一步就把對應
的 skip 換成實作。用 ``skip`` 而非 ``xfail``：這些步驟不是「預期失敗的已知
bug」，是「尚未存在的功能」——reason 欄直接寫明等哪個工作包，交接時不用猜。

步驟間共享狀態（token、document id…）在 2–5 步實作時改成 class 級 fixture 串接；
骨架階段不預先搭那個架子（YAGNI——上傳的回應形狀定案前，架子必然是猜的）。
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import SmokeTenant

_TIMEOUT_S = 10.0


class TestSmokeLoop:
    def test_step_1_login_and_whoami(self, api_server: str, smoke_tenant: SmokeTenant) -> None:
        """登入拿 access token，並以它取回自己的身分。

        兩段都要打：只驗 login 200 的話，「token 簽出來但驗不回去」（金鑰對不
        一致、audience 錯）會全綠——那正是跨 session 最容易踩壞的一類設定。
        """
        login = httpx.post(
            f"{api_server}/api/v1/auth/login",
            json={
                "tenant_slug": smoke_tenant.slug,
                "email": smoke_tenant.email,
                "password": smoke_tenant.password,
            },
            timeout=_TIMEOUT_S,
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]

        me = httpx.get(
            f"{api_server}/api/v1/users/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_S,
        )
        assert me.status_code == 200, me.text
        assert me.json()["email"] == smoke_tenant.email

    @pytest.mark.skip(reason="等 1B：文件上傳端點（KB/Document CRUD + 單請求上傳）")
    def test_step_2_upload_document(self) -> None: ...

    @pytest.mark.skip(reason="等 1B/1C：ETL 狀態機 + embedding worker（文件轉 ready）")
    def test_step_3_document_becomes_ready(self) -> None: ...

    @pytest.mark.skip(reason="等 1D：Chat SSE 問答")
    def test_step_4_ask_question(self) -> None: ...

    @pytest.mark.skip(reason="等 1D：回答含 citation 且指向已上傳文件")
    def test_step_5_answer_has_citation(self) -> None: ...
