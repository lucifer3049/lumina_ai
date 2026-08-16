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

import json
import time
from dataclasses import dataclass

import httpx
import pytest

from tests.e2e.conftest import SmokeTenant

_TIMEOUT_S = 10.0

# 一份小文件的 ETL 應在數秒內完成；60 秒是「worker 沒起來 / 卡住」的止血點，
# 不是效能目標（那是 11_NFR 的事）。
_ETL_TIMEOUT_S = 60.0

# 一次生成（MockProvider）應在一秒內結束；30 秒是「背景 task 沒跑起來」的止血點。
_STREAM_TIMEOUT_S = 30.0

_SMOKE_DOCUMENT = """# Smoke 測試文件

這份文件由 smoke suite 上傳，用來驗證整條 ETL 走得通。

## 第一節

內容需要足夠長，切塊之後至少會有一個 chunk 可供檢索。
""".encode()


@dataclass
class _SmokeState:
    """步驟之間傳遞的狀態（token、document id）。

    骨架階段（1A-5）刻意沒有預先搭這個架子——上傳的回應形狀當時還沒定案。現在定案了，
    用一個 session 級的可變物件串接：pytest 的 fixture 之間不能回傳「上一個測試算出來
    的值」，而把每一步都重跑一次前置（登入、建 KB、上傳）會讓 smoke 慢好幾倍。
    """

    tenant: SmokeTenant
    token: str = ""
    document_id: str = ""
    conversation_id: str = ""
    message_id: str = ""


@pytest.fixture(scope="session")
def smoke_state(smoke_tenant: SmokeTenant) -> _SmokeState:
    return _SmokeState(tenant=smoke_tenant)


def _read_stream(
    api_server: str, headers: dict[str, str], stream_url: str
) -> list[tuple[str, dict[str, object]]]:
    """把 SSE 讀成 [(事件名, payload)]。心跳（`:` 開頭）與空行直接略過。"""
    events: list[tuple[str, dict[str, object]]] = []
    with httpx.stream(
        "GET",
        f"{api_server}{stream_url}",
        headers={**headers, "Accept": "text/event-stream"},
        timeout=_STREAM_TIMEOUT_S,
    ) as response:
        assert response.status_code == 200, response.read()
        name = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((name, json.loads(line.removeprefix("data: "))))
    return events


def _login(api_server: str, tenant: SmokeTenant) -> str:
    response = httpx.post(
        f"{api_server}/api/v1/auth/login",
        json={
            "tenant_slug": tenant.slug,
            "email": tenant.email,
            "password": tenant.password,
        },
        timeout=_TIMEOUT_S,
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


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

    def test_step_2_upload_document(self, api_server: str, smoke_state: _SmokeState) -> None:
        """建 KB → 上傳一份 Markdown → 拿到 document id。

        走 multipart 真的送位元組（09 §3.1 的小檔路徑），不是造一列 DB：這一步要驗的
        是「上傳端點 + 物件儲存 + 內容判定」串起來還活著。
        """
        token = _login(api_server, smoke_state.tenant)
        headers = {"Authorization": f"Bearer {token}"}

        created = httpx.post(
            f"{api_server}/api/v1/knowledge-bases",
            headers=headers,
            json={"name": "Smoke KB"},
            timeout=_TIMEOUT_S,
        )
        assert created.status_code == 201, created.text
        kb_id = created.json()["id"]

        uploaded = httpx.post(
            f"{api_server}/api/v1/knowledge-bases/{kb_id}/documents",
            headers=headers,
            files={"file": ("smoke.md", _SMOKE_DOCUMENT, "text/markdown")},
            timeout=_TIMEOUT_S,
        )
        assert uploaded.status_code == 201, uploaded.text
        body = uploaded.json()
        assert body["status"] == "uploaded"

        smoke_state.token = token
        smoke_state.document_id = body["id"]

    def test_step_3_document_is_ready(
        self, api_server: str, background_worker: None, smoke_state: _SmokeState
    ) -> None:
        """ETL + embedding 把文件推進到 ``ready``（08 §2 的狀態機）。

        1C-3 之前這一步停在 ``chunked``（``ready`` 要等 embedding）。現在等的是完整
        的寫路徑：**兩條佇列、兩個任務、一次任務交棒**。停在 chunked 就逾時，那正是
        「ETL 跑完卻沒有人接手」的症狀——它不會有任何錯誤訊息。

        輪詢而非固定 sleep：ETL 的時間隨文件大小變動，固定等待不是太慢就是會偶爾紅。
        """
        assert smoke_state.document_id, "第 2 步沒有留下 document id"
        headers = {"Authorization": f"Bearer {smoke_state.token}"}
        deadline = time.monotonic() + _ETL_TIMEOUT_S

        while True:
            response = httpx.get(
                f"{api_server}/api/v1/documents/{smoke_state.document_id}",
                headers=headers,
                timeout=_TIMEOUT_S,
            )
            assert response.status_code == 200, response.text
            document = response.json()
            if document["status"] in {"ready", "failed"}:
                break
            assert time.monotonic() < deadline, f"ETL {_ETL_TIMEOUT_S}s 內未完成：{document}"
            time.sleep(1.0)

        assert document["status"] == "ready", document

    def test_step_4_ask_question(self, api_server: str, smoke_state: _SmokeState) -> None:
        """建立對話 → 送出問題 → 讀完串流（1D-4a）。

        **這一步走的是真的部署形狀**：POST 建立回合之後，生成跑在伺服器的背景 task 上，
        而我們從另一個請求（GET）把事件讀回來。在測試行程內直接呼叫 service 驗不到
        「背景 task 沒被 GC 掉」「緩衝區跨請求讀得到」這兩種故障，而它們的症狀都是
        「訊息永遠停在 streaming」。
        """
        headers = {"Authorization": f"Bearer {smoke_state.token}"}

        created = httpx.post(
            f"{api_server}/api/v1/conversations",
            headers=headers,
            json={"title": "smoke"},
            timeout=_TIMEOUT_S,
        )
        assert created.status_code == 201, created.text
        conversation_id = created.json()["id"]

        turn = httpx.post(
            f"{api_server}/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
            json={"content": "這份文件在說什麼？"},
            timeout=_TIMEOUT_S,
        )
        assert turn.status_code == 201, turn.text
        smoke_state.conversation_id = conversation_id
        smoke_state.message_id = turn.json()["message_id"]

        events = _read_stream(api_server, headers, turn.json()["stream_url"])

        assert events[0][0] == "meta"
        assert events[-1][0] == "done", f"串流沒有正常收尾：{events[-1]}"
        answer = "".join(str(data["text"]) for name, data in events if name == "delta")
        assert answer.strip(), "回答是空的"

        message = httpx.get(
            f"{api_server}/api/v1/conversations/{conversation_id}/messages",
            headers=headers,
            timeout=_TIMEOUT_S,
        ).json()["items"][-1]
        assert message["status"] == "completed", message
        assert message["content"] == answer, "串流看到的與存下來的不一致"

    @pytest.mark.skip(reason="等 1D-5：RAG 編排與 citation")
    def test_step_5_answer_has_citation(self) -> None: ...
