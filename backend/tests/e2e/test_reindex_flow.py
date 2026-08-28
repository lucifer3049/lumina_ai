"""E2E：改切塊參數 → 重建 → 文件重新切過且知識庫回到「不需要重建」（2B-6 缺口①）。

**這是 2B-6 唯一一條沒有被端到端驗證過的路**。integration 驗得到「文件被送進
re-ingest」與「還沒跑完就不切換」，但驗不到重切階段真正在等的那件事：**別人做完**
——ETL 把文件重新解析、切塊，embedding 把新 chunk 算完，然後 reindex 才推得動。

那條交棒橫跨三個佇列（`reindex` → `etl` → `embedding` → 回到 `reindex`）與兩個行程，
而它斷掉的每一種形狀都沒有錯誤訊息：

1. **worker 沒聽 `reindex` 佇列**——job 永遠停在 `pending`，API 全部 200（1C-3 踩過
   同型的洞：worker 起來了但只吃 `etl`）。
2. **重切階段永遠等不到**——`advance` 判斷「還在跑嗎」用的是文件狀態，判錯的話它要嘛
   卡死，要嘛在文件還沒 ready 時就進 embedding 並把一個空的知識庫切換上線。
3. **心跳沒推**——job 合法地等 ETL 的那段期間看起來像停滯，補償掃描會把它判死
   （見 `tests/integration/test_kb_reindex_rescue.py`）。

放 e2e 而不是 integration：以上三種全部只在「真的有另一個行程在跑」時才成立。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import pytest

from tests.e2e.conftest import SmokeTenant

_TIMEOUT_S = 10.0
# 一份小 Markdown 的完整重建（重切 + 重算）在本機是數秒。90 秒是「某一段沒有人接手」
# 的止血點，不是效能目標——超過它代表交棒斷了，而不是慢。
_REINDEX_TIMEOUT_S = 90.0
_ETL_TIMEOUT_S = 60.0

_DOCUMENT = """# 重建測試文件

這份文件用來驗證「改了切塊參數之後，既有內容真的會被重新切過」。

## 第一節 背景

內容需要夠長，才切得出不只一個 chunk：切塊是以 token 數為單位的，而一份只有兩行的
文件不論參數怎麼調都只會產生同一個 chunk——那樣的話這條 e2e 會全綠，卻什麼都沒驗到。

## 第二節 細節

重建的四步分別是：建立目標而不動現行值、背景批次算新版向量、完成度 100% 時原子切換、
以及觀察期過後清掉舊版。這一段的存在只是為了讓文件長到足以被切成好幾塊。

## 第三節 附註

再補一段，理由同上。重建之後這份內容必須仍然檢索得到——那是這條 e2e 真正的斷言，
而不只是「狀態變成 completed」。
""".encode()


@dataclass
class _State:
    tenant: SmokeTenant
    token: str = ""
    kb_id: str = ""
    document_id: str = ""


@pytest.fixture(scope="module")
def state(smoke_tenant: SmokeTenant) -> _State:
    return _State(tenant=smoke_tenant)


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


def _wait_for(check: object, *, deadline_s: float, what: str) -> object:
    """輪詢而非固定 sleep（同 smoke 第 3 步的理由）。"""
    deadline = time.monotonic() + deadline_s
    while True:
        result = check()  # type: ignore[operator]
        if result is not None:
            return result
        assert time.monotonic() < deadline, f"{what} 在 {deadline_s}s 內沒有完成"
        time.sleep(1.0)


class TestReindexAfterChunkConfigChange:
    def test_step_1_upload_and_ready(
        self, api_server: str, background_worker: None, state: _State
    ) -> None:
        token = _login(api_server, state.tenant)
        headers = {"Authorization": f"Bearer {token}"}

        created = httpx.post(
            f"{api_server}/api/v1/knowledge-bases",
            headers=headers,
            json={"name": "Reindex KB"},
            timeout=_TIMEOUT_S,
        )
        assert created.status_code == 201, created.text
        kb_id = created.json()["id"]

        uploaded = httpx.post(
            f"{api_server}/api/v1/knowledge-bases/{kb_id}/documents",
            headers=headers,
            files={"file": ("reindex.md", _DOCUMENT, "text/markdown")},
            timeout=_TIMEOUT_S,
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["id"]

        def _ready() -> dict[str, object] | None:
            response = httpx.get(
                f"{api_server}/api/v1/documents/{document_id}",
                headers=headers,
                timeout=_TIMEOUT_S,
            )
            assert response.status_code == 200, response.text
            body: dict[str, object] = response.json()
            return body if body["status"] in {"ready", "failed"} else None

        document = _wait_for(_ready, deadline_s=_ETL_TIMEOUT_S, what="首次 ETL")
        assert document["status"] == "ready", document  # type: ignore[index]

        state.token = token
        state.kb_id = kb_id
        state.document_id = document_id

    def test_step_2_changing_chunk_config_asks_for_a_reindex(
        self, api_server: str, state: _State
    ) -> None:
        """2B-5 的 `knowledge_version` → 2B-6 的 `needs_reindex`，走的是真的 HTTP。"""
        assert state.kb_id, "第 1 步沒有留下 kb id"
        headers = {"Authorization": f"Bearer {state.token}"}

        patched = httpx.patch(
            f"{api_server}/api/v1/knowledge-bases/{state.kb_id}",
            headers=headers,
            json={"config": {"chunk": {"target_tokens": 128, "overlap_tokens": 16}}},
            timeout=_TIMEOUT_S,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["needs_reindex"] is True, "改了切塊參數卻沒有人提示要重建"

    def test_step_3_reindex_runs_to_completion(self, api_server: str, state: _State) -> None:
        """整條交棒：`reindex` → `etl` → `embedding` → 回到 `reindex` 完成切換。"""
        headers = {"Authorization": f"Bearer {state.token}"}

        started = httpx.post(
            f"{api_server}/api/v1/knowledge-bases/{state.kb_id}/reindex",
            headers=headers,
            json={},
            timeout=_TIMEOUT_S,
        )
        assert started.status_code == 202, started.text
        assert started.json()["rechunk"] is True

        def _finished() -> dict[str, object] | None:
            response = httpx.get(
                f"{api_server}/api/v1/knowledge-bases/{state.kb_id}/reindex",
                headers=headers,
                timeout=_TIMEOUT_S,
            )
            assert response.status_code == 200, response.text
            body: dict[str, object] = response.json()
            return body if body["status"] in {"completed", "failed"} else None

        job = _wait_for(_finished, deadline_s=_REINDEX_TIMEOUT_S, what="重建")
        assert job["status"] == "completed", job  # type: ignore[index]
        assert job["switched_at"], "完成了卻沒有切換時間——第 4 步的保留窗會從 None 起算"  # type: ignore[index]

    def test_step_4_the_content_was_really_re_cut(self, api_server: str, state: _State) -> None:
        """**這才是重點**：狀態變成 completed 不代表內容真的被重切過。

        只驗狀態的話，一個「什麼都沒做就宣告完成」的實作會全綠——而那正是重切階段
        最可能的失敗形狀（判斷「還在跑嗎」判錯，於是直接跳到完成）。
        """
        headers = {"Authorization": f"Bearer {state.token}"}

        document = httpx.get(
            f"{api_server}/api/v1/documents/{state.document_id}",
            headers=headers,
            timeout=_TIMEOUT_S,
        )
        assert document.status_code == 200, document.text
        assert document.json()["status"] == "ready", "重建之後文件必須回到可檢索的狀態"
        assert document.json()["doc_version"] == 2, "重切走的是 re-ingest，版本必須 +1"

        # **重建之後知識庫還查得到東西**——這一條擋的是最貴的失敗：重切把舊 chunk
        # 標成 superseded 退出檢索，而新的那批如果沒有被算成向量（交棒斷在
        # embedding 那一段），知識庫會變成一個狀態全綠、卻什麼都答不出來的空殼。
        #
        # 09 §2.3 的 `GET /documents/{id}/chunks` 至今沒有實作，所以不比對 chunk 數；
        # 檢索端點證明的更多：新 chunk 被切出來、被算成向量、而且進得了候選集。
        retrieved = httpx.post(
            f"{api_server}/api/v1/rag/query",
            headers=headers,
            json={"kb_id": state.kb_id, "query": "重建的四步分別是什麼"},
            timeout=_TIMEOUT_S,
        )
        assert retrieved.status_code == 200, retrieved.text
        assert retrieved.json()["items"], "重建之後檢索回不到任何內容——知識庫被清空了"

        kb = httpx.get(
            f"{api_server}/api/v1/knowledge-bases/{state.kb_id}",
            headers=headers,
            timeout=_TIMEOUT_S,
        )
        assert kb.status_code == 200, kb.text
        assert kb.json()["needs_reindex"] is False, (
            "重建完成後提示必須消失，否則設定畫面會永遠顯示「需要重建」"
        )
