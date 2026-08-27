"""驗收：`rag_trace` 的關聯與對外形狀（06 §7、09 §2.3、12 §1.1，工作包 2B-5）。

12 §1.1 的 Correlation 那一條把話說死了：「SSE 事件、audit、usage_logs、**rag_trace**
全部帶 request_id——一個 ID 查穿全鏈路」。而 `rag_trace` 是這串裡唯一還不存在的。

本檔驗三件事，`tests/integration/test_rag_trace.py` 驗的是 trace 的內容：

1. **request_id 真的在上面**，而且與那次請求的存取日誌是同一個。
2. **問答那條路也在上面**——它跑在**背景 task** 裡（1D-4a 的第二段），而
   `RequestContextMiddleware` 的 finally 早就把請求的 context 收掉了。contextvars 在
   `create_task` 當下複製一份給子 task，所以父 task 的清理不會波及它；**但這件事一旦
   哪天被改成別的併發寫法就會靜靜地失效**，而症狀是 trace 上的 request_id 變成 null
   ——那時 log 還在、欄位還在，只是再也串不起來。
3. **`/rag/query` 說得出這一趟降級了沒有**。`api/v1/rag.py` 自 2B-3 起就掛著一句
   「degraded 暫時不出現在回應裡……排在 2B 結案的文件同步」。不補的話，除錯端點回
   一組很差的結果時，呼叫端分不出是「檢索真的差」還是「TEI 容器沒開」——而那兩者
   在畫面上長得一模一樣（2B-4 結案缺口⑥的同一個問題）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest

from ai.gateway import AIGateway
from ai.gateway.chat import ChatRequest, ChatTimeouts, DoneDelta, ProviderDelta, TextDelta
from ai.gateway.providers.mock import MockChatProvider, MockEmbeddingProvider
from api.main import REQUEST_ID_HEADER, create_app
from common.passwords import hash_password
from config.logging import configure_logging
from core.redis import get_redis, tenant_key
from rag.citation import marker_for
from services.knowledge.embedding import EmbeddingService
from tests.conftest import TENANT_A
from tests.factories.conversation import make_conversation
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_chunk, make_document, make_knowledge_base
from tests.seed import ensure_identity_seed, ensure_prompt_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"
OWNER_EMAIL = "owner@example.com"
TRACE_EVENT = "rag_trace"
ACCESS_EVENT = "http_request"

_CHUNK_TEXT = "員工每年特別休假 14 天，應於三日前提出申請。"


def _events(captured: str, name: str) -> list[dict[str, Any]]:
    lines = []
    for line in captured.splitlines():
        if not line.strip().startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover —— 非 JSON 的第三方輸出
            continue
        if parsed.get("event") == name:
            lines.append(parsed)
    return lines


@pytest.fixture(autouse=True)
async def _drain_background_generation() -> AsyncIterator[None]:
    """等背景生成收工再讓測試結束（理由見 test_chat_stream.py 的同名 fixture）。

    這一檔尤其需要：要驗的正是背景那條路寫出來的 log，沒有 drain 的話它可能在
    assert 之後才寫出來，測試會**間歇性**地紅——而看起來像 flaky。
    """
    yield
    from api.background import drain

    await drain(timeout_seconds=10)


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def owner() -> uuid.UUID:
    from apps.identity.models import Role

    ensure_identity_seed()
    ensure_prompt_seed()
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        user = make_user(
            tenant_id=TENANT_A, email=OWNER_EMAIL, password_hash=hash_password(PASSWORD)
        )
        make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name="owner"))
    return uuid.UUID(str(user.id))


@pytest.fixture
def kb_id() -> uuid.UUID:
    with tenant_scope(TENANT_A):
        kb = make_knowledge_base(tenant_id=TENANT_A)
        document = make_document(kb=kb, filename="人事規章.pdf", status="chunked")
        make_chunk(document=document, seq=0, content=_CHUNK_TEXT, meta={"page": 7})
        kb_uuid = uuid.UUID(str(kb.id))
        document_id = uuid.UUID(str(document.id))

    EmbeddingService(
        gateway=AIGateway(embedding_provider=MockEmbeddingProvider(), retry_backoff_seconds=())
    ).embed_document(TENANT_A, document_id)
    return kb_uuid


@pytest.fixture
def conversation(owner: uuid.UUID, kb_id: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        row = make_conversation(tenant_id=TENANT_A, user_id=owner, kb_ids=[kb_id])
    return uuid.UUID(str(row.id))


@pytest.fixture
def plain_conversation(owner: uuid.UUID) -> uuid.UUID:
    with tenant_scope(TENANT_A):
        row = make_conversation(tenant_id=TENANT_A, user_id=owner, kb_ids=[])
    return uuid.UUID(str(row.id))


@pytest.fixture
def answer_with_a_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型回一個真的引用標記 + 一個**捏造的**。

    捏造的那個要被剔掉（06 §3.3 的幻覺防線），而「剔掉了幾個」正是 trace 要記的
    citation 驗證結果——它是 13 §3.5 第 1 項唯一的量測。
    """

    async def stream_chat(
        self: Any, request: ChatRequest, *, timeouts: ChatTimeouts
    ) -> AsyncIterator[ProviderDelta]:
        yield TextDelta(text=f"每年 14 天[c:{marker_for(0)}]，另見[c:{marker_for(9)}]。")
        yield DoneDelta(finish_reason="stop")

    monkeypatch.setattr(MockChatProvider, "stream_chat", stream_chat)


@pytest.fixture
async def client(capsys: pytest.CaptureFixture[str]) -> AsyncIterator[httpx.AsyncClient]:
    """在 capsys 生效後才設定 logging——handler 綁的是當下的 sys.stdout
    （同 test_request_logging.py）。"""
    configure_logging(level="INFO", fmt="json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": OWNER_EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _query(
    client: httpx.AsyncClient, token: str, kb_id: uuid.UUID, **body: object
) -> httpx.Response:
    return await client.post(
        "/api/v1/rag/query",
        headers=_auth(token),
        json={"kb_id": str(kb_id), "query": "年假幾天", **body},
    )


async def _ask(client: httpx.AsyncClient, token: str, conversation_id: uuid.UUID) -> None:
    created = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=_auth(token),
        json={"content": "年假幾天？"},
    )
    assert created.status_code == 201, created.text
    message_id = created.json()["message_id"]
    async with client.stream(
        "GET",
        f"/api/v1/conversations/{conversation_id}/messages/{message_id}/stream",
        headers={**_auth(token), "Accept": "text/event-stream"},
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for _ in response.aiter_lines():
            pass


class TestQueryEndpointExposesIt:
    async def test_the_response_reports_degradation(
        self, client: httpx.AsyncClient, owner: uuid.UUID, kb_id: uuid.UUID
    ) -> None:
        """`api/v1/rag.py` 欠的那一筆。正常路徑是**空清單而不是省略欄位**：省略的話，
        「這一趟沒有降級」與「這個版本還沒有這個欄位」在呼叫端分不出來（同
        `usage.rag.degraded` 的處置）。"""
        response = await _query(client, await _token(client), kb_id)

        assert response.status_code == 200, response.text
        assert response.json()["degraded"] == []

    async def test_the_response_carries_a_trace_summary(
        self, client: httpx.AsyncClient, owner: uuid.UUID, kb_id: uuid.UUID
    ) -> None:
        """這個端點存在的唯一理由是「看檢索到底準不準」（見 api/v1/rag.py 的
        docstring）。只回一串命中的話，看得到結果、看不到過程——而「為什麼是這個
        順序」正是要看的東西。"""
        response = await _query(client, await _token(client), kb_id)

        trace = response.json()["trace"]
        assert trace["mode"] == "vector"
        assert trace["fused_count"] == 1
        assert [route["name"] for route in trace["routes"]] == ["vector"]
        assert set(trace["stages"]) >= {"embed", "vector", "fuse"}

    async def test_the_trace_summary_carries_no_chunk_content(
        self, client: httpx.AsyncClient, owner: uuid.UUID, kb_id: uuid.UUID
    ) -> None:
        """內文已經在 `items` 裡了。trace 再帶一份的話，回應大小會隨 top_k 翻倍，
        而那一份沒有任何新資訊。"""
        response = await _query(client, await _token(client), kb_id)

        assert _CHUNK_TEXT not in json.dumps(response.json()["trace"], ensure_ascii=False)

    async def test_the_openapi_declares_the_new_fields(self, client: httpx.AsyncClient) -> None:
        """`operation_id` 與回應形狀視同 API 契約（CLAUDE.md 測試規範）——前端的
        generated client 是照它產的，漏宣告的話型別對不上而且沒有人會發現。"""
        schema = (await client.get("/openapi.json")).json()
        properties = schema["components"]["schemas"]["RagQueryOut"]["properties"]

        assert {"items", "degraded", "trace"} <= set(properties)


class TestCorrelation:
    async def test_the_trace_shares_the_request_id_with_the_access_log(
        self,
        client: httpx.AsyncClient,
        owner: uuid.UUID,
        kb_id: uuid.UUID,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """12 §1.1：一個 ID 查穿全鏈路。對不上的話，維運手上拿著使用者回報的那個 id，
        卻撈不到那一次檢索——而 log 看起來完全正常。"""
        response = await _query(client, await _token(client), kb_id)

        captured = capsys.readouterr().out
        traces = _events(captured, TRACE_EVENT)
        assert len(traces) == 1
        assert traces[0]["request_id"] == response.headers[REQUEST_ID_HEADER]
        assert traces[0]["tenant_id"] == str(TENANT_A)

    async def test_the_background_generation_keeps_the_request_id(
        self,
        client: httpx.AsyncClient,
        conversation: uuid.UUID,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """**最容易漏的一條。** 生成跑在背景 task（1D-4a），而請求早就回了——
        `RequestContextMiddleware` 的 finally 已經把 contextvars 收掉。

        現在能對得起來是因為 `create_task` 會複製當下的 context；改成 threadpool、
        改成 Celery、或在建立 task 之前先清理，這一條都會斷。而斷掉之後 trace 照常
        寫出來，只是 request_id 是 null——沒有這條測試的話，發現的時機是「出事那天
        查不到」。
        """
        token = await _token(client)
        created = await client.post(
            f"/api/v1/conversations/{conversation}/messages",
            headers=_auth(token),
            json={"content": "年假幾天？"},
        )
        request_id = created.headers[REQUEST_ID_HEADER]
        message_id = created.json()["message_id"]
        async with client.stream(
            "GET",
            f"/api/v1/conversations/{conversation}/messages/{message_id}/stream",
            headers={**_auth(token), "Accept": "text/event-stream"},
        ) as response:
            async for _ in response.aiter_lines():
                pass

        traces = _events(capsys.readouterr().out, TRACE_EVENT)
        assert len(traces) == 1
        # 建立回合的那一個請求，不是串流那一個：檢索發生在生成裡，而生成是那一次
        # POST 建立起來的。
        assert traces[0]["request_id"] == request_id

    async def test_one_turn_produces_exactly_one_trace(
        self,
        client: httpx.AsyncClient,
        conversation: uuid.UUID,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """一次問答一筆。檢索與收尾各寫一筆的話，「這個月有多少次查詢降級了」的
        分母會憑空變成兩倍——而兩筆都長得像真的。"""
        await _ask(client, await _token(client), conversation)

        assert len(_events(capsys.readouterr().out, TRACE_EVENT)) == 1

    async def test_a_chat_without_a_kb_writes_no_trace(
        self,
        client: httpx.AsyncClient,
        plain_conversation: uuid.UUID,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """純閒聊路徑（06 §9）沒有檢索。記一筆空的會汙染所有比例型指標的分母
        （同 `usage.rag` 的處置）。"""
        await _ask(client, await _token(client), plain_conversation)

        assert _events(capsys.readouterr().out, TRACE_EVENT) == []


class TestCitationResults:
    async def test_the_trace_records_the_citation_verification(
        self,
        client: httpx.AsyncClient,
        conversation: uuid.UUID,
        answer_with_a_citation: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """06 §7 的最後一項：citation 驗證結果與檢索**在同一筆 trace 上**。

        分成兩筆的話，「這一題檢索到什麼、模型引用了什麼、幾個是捏造的」要靠人去對
        時間戳——而那正是 rag_trace 存在的理由。

        模型回了一個真的標記與一個捏造的，所以是 1 個有效引用、1 個被剔除。
        """
        await _ask(client, await _token(client), conversation)

        trace = _events(capsys.readouterr().out, TRACE_EVENT)[0]
        assert trace["citations"] == {"citations": 1, "dropped": 1}

    async def test_the_trace_and_the_stored_usage_agree(
        self,
        client: httpx.AsyncClient,
        conversation: uuid.UUID,
        answer_with_a_citation: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """`usage.rag`（落在 `messages` 上，1D-5）與 trace（落在 log 上）記的是同一件
        事。兩份數字漂掉時，3B 的評測報表與除錯會給出互相矛盾的答案，而沒有人知道
        該信哪一份。"""
        from apps.conversation.models import Message
        from core.db import run_orm

        await _ask(client, await _token(client), conversation)
        trace = _events(capsys.readouterr().out, TRACE_EVENT)[0]

        def _latest() -> Any:
            with tenant_scope(TENANT_A):
                return (
                    Message.objects.filter(conversation_id=conversation, role="assistant")
                    .order_by("-created_at")
                    .first()
                )

        message = await run_orm(_latest)
        assert message.usage["rag"]["citations"] == trace["citations"]["citations"]
        assert message.usage["rag"]["dropped"] == trace["citations"]["dropped"]
        assert message.usage["rag"]["degraded"] == trace["degraded"]
