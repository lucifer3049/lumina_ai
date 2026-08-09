"""驗收：knowledge 端點的權限矩陣（10 §3、09 §2.3）。

與 `test_permission_enforcement.py`（identity 那組）同一個型式：**矩陣式**而不是逐條
寫測試。權限是「角色 × 端點」的二維問題，逐條寫必然會漏，而漏掉的那一格通常就是被
放行的那一個。列成表之後，新增端點忘了宣告權限會讓表少一列，加角色會讓表少一欄。

本檔與 identity 那組**刻意分開**：混在同一張表裡，1B 以後每個工作包都要回頭改
identity 的測試檔，而那張表會長到沒有人讀得完。分開的代價是四個角色的 fixture 重複
一次；用「表各自維護、fixture 各自建」換「每個 context 的權限一眼看得完」。

**Editor 與 Viewer 的差別在這裡第一次出現**（1A 時兩者權限相同）：Viewer 讀得到
知識庫，但不能建、不能改、不能刪。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from api.main import create_app
from apps.identity.models import Role
from common.passwords import hash_password
from core.redis import get_redis, tenant_key
from tests.conftest import TENANT_A
from tests.factories.identity import make_tenant, make_user, make_user_role, tenant_scope
from tests.factories.knowledge import make_document, make_knowledge_base
from tests.seed import ensure_identity_seed

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

PASSWORD = "correct horse battery staple"
SLUG_A = "tenant-a"

ALLOWED = 200
CREATED = 201
NO_CONTENT = 204
FORBIDDEN = 403

# 上傳那一列的 body 不是 JSON。用一個哨兵值標記，由 test_permission_matrix 轉成
# multipart——把 multipart 的細節塞進矩陣會讓那張表變得難讀，而表的可讀性正是
# 它存在的理由。
UPLOAD = "<multipart-upload>"
# 每個角色各自上傳一份不同內容：同內容第二次會被去重擋成 409（05 §3.2），那會讓
# 這一列的預期狀態碼隨執行順序而變。
#
# 用串接而不是 %-格式化：PDF 的魔術字本身就是 `%PDF`，那個 `%P` 會被當成格式指示字
# 而 ValueError（實際踩過）。
PDF_PREFIX = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nrole="

# 端點 × 角色 → 預期狀態碼。路徑中的 ``{kb}`` / ``{doc}`` 由 fixture 填入。
#
# 只列狀態碼、不驗回應內容：這張表回答的是「擋不擋得住」，內容正確性由
# test_knowledge_endpoints.py 負責。
PERMISSION_MATRIX = [
    (
        "GET",
        "/api/v1/knowledge-bases",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
    (
        "POST",
        "/api/v1/knowledge-bases",
        {"name": "新的 KB"},
        {"owner": CREATED, "admin": CREATED, "editor": CREATED, "viewer": FORBIDDEN},
    ),
    (
        "GET",
        "/api/v1/knowledge-bases/{kb}",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
    # PATCH 改的是 chunk 策略與檢索參數（config），那會讓整個 KB 需要重算——
    # 維運等級的動作，因此要 knowledge:admin 而不是 write。
    (
        "PATCH",
        "/api/v1/knowledge-bases/{kb}",
        {"name": "改名"},
        {"owner": ALLOWED, "admin": ALLOWED, "editor": FORBIDDEN, "viewer": FORBIDDEN},
    ),
    (
        "GET",
        "/api/v1/knowledge-bases/{kb}/documents",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
    # 上傳（1B-3）。這一列的 body 是 multipart 而不是 JSON，由下方的 test 特別處理
    # ——放進矩陣是為了讓「新端點必須進矩陣」的反查測試涵蓋得到它。
    (
        "POST",
        "/api/v1/knowledge-bases/{kb}/documents",
        UPLOAD,
        {"owner": CREATED, "admin": CREATED, "editor": CREATED, "viewer": FORBIDDEN},
    ),
    (
        "GET",
        "/api/v1/documents/{doc}",
        None,
        {"owner": ALLOWED, "admin": ALLOWED, "editor": ALLOWED, "viewer": ALLOWED},
    ),
    # 刪文件是 knowledge:write（Editor 的日常工作），不是 admin：文件進出是編輯者
    # 的職責，而它的破壞範圍限於單一文件、且是軟刪除（30 天內可救）。
    (
        "DELETE",
        "/api/v1/documents/{doc}",
        None,
        {"owner": NO_CONTENT, "admin": NO_CONTENT, "editor": NO_CONTENT, "viewer": FORBIDDEN},
    ),
    # 刪 KB 連帶整個知識庫的文件失效，破壞範圍與 PATCH config 同級。
    (
        "DELETE",
        "/api/v1/knowledge-bases/{kb}",
        None,
        {"owner": NO_CONTENT, "admin": NO_CONTENT, "editor": FORBIDDEN, "viewer": FORBIDDEN},
    ),
]


@pytest.fixture(autouse=True)
def _clean_redis() -> Iterator[None]:
    yield
    client = get_redis()
    keys = list(client.scan_iter(match=tenant_key(TENANT_A, "*")))
    if keys:
        client.delete(*keys)


@pytest.fixture
def scenario() -> dict[str, object]:
    """租戶 A 內四個角色各一個使用者，加一個 KB 與一份文件。

    同步 fixture：ORM 是同步的，在 async 測試函式裡直接建資料會被 Django 擋下。
    """
    ensure_identity_seed()
    emails: dict[str, str] = {}
    with tenant_scope(TENANT_A):
        make_tenant(id=TENANT_A, slug=SLUG_A)
        for role_name in ("owner", "admin", "editor", "viewer"):
            email = f"{role_name}@example.com"
            user = make_user(tenant_id=TENANT_A, email=email, password_hash=hash_password(PASSWORD))
            make_user_role(user=user, role=Role.objects.get(tenant__isnull=True, name=role_name))
            emails[role_name] = email
        kb = make_knowledge_base(tenant_id=TENANT_A, name="既有 KB")
        document = make_document(kb=kb, filename="existing.pdf")

    return {"emails": emails, "kb": kb.id, "document": document.id}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as c:
        yield c


async def _token_for(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_slug": SLUG_A, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, f"{email} 登入失敗：{response.text}"
    return str(response.json()["access_token"])


@pytest.mark.parametrize(("method", "path", "body", "expectations"), PERMISSION_MATRIX)
@pytest.mark.parametrize("role", ["owner", "admin", "editor", "viewer"])
async def test_permission_matrix(
    client: httpx.AsyncClient,
    scenario: dict[str, object],
    role: str,
    method: str,
    path: str,
    # str 是 UPLOAD 哨兵（multipart 那一列）；dict 是 JSON body；None 是無 body。
    body: dict[str, object] | str | None,
    expectations: dict[str, int],
) -> None:
    emails = scenario["emails"]
    assert isinstance(emails, dict)
    token = await _token_for(client, emails[role])
    url = path.format(kb=scenario["kb"], doc=scenario["document"])
    auth = {"Authorization": f"Bearer {token}"}

    if body == UPLOAD:
        response = await client.post(
            url,
            files={"file": (f"{role}.pdf", PDF_PREFIX + role.encode(), "application/pdf")},
            headers=auth,
        )
    else:
        response = await client.request(method, url, json=body, headers=auth)

    assert response.status_code == expectations[role], (
        f"{role} 對 {method} {path} 得到 {response.status_code}，"
        f"預期 {expectations[role]}：{response.text[:200]}"
    )


async def test_forbidden_response_uses_the_contract_error_code(
    client: httpx.AsyncClient, scenario: dict[str, object]
) -> None:
    """403 的 body 必須是契約裡的 ``PERMISSION_DENIED``（09 附錄 A）。

    client 依 code 分支而不是依狀態碼——同樣是 403，「權限不足」與「租戶被停權」的
    處理完全不同（前者去找管理員，後者要聯絡業務）。
    """
    emails = scenario["emails"]
    assert isinstance(emails, dict)
    token = await _token_for(client, emails["viewer"])

    response = await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "viewer 不該建得起來"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_permission_denial_is_audited(
    client: httpx.AsyncClient,
    scenario: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """被拒的權限要留紀錄，且帶上**被拒的 permission code**（10 §3）。

    沒有這筆紀錄的話，「有人在嘗試越權」與「某個角色的權限設錯了」在維運端看起來
    完全一樣——都只是使用者回報「我打不開這個頁面」。
    """
    from config.logging import configure_logging

    configure_logging(level="INFO", fmt="json")
    emails = scenario["emails"]
    assert isinstance(emails, dict)
    token = await _token_for(client, emails["viewer"])

    await client.post(
        "/api/v1/knowledge-bases",
        json={"name": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert "knowledge:write" in capsys.readouterr().out


async def test_unauthenticated_requests_are_401_not_403(
    client: httpx.AsyncClient, scenario: dict[str, object]
) -> None:
    """沒帶 token 是 401（AUTH_REQUIRED），不是 403。

    兩者的意思不同：401 是「你是誰」，403 是「你不能」。前端據此決定要導向登入頁
    還是顯示「請洽管理員」，混用會讓 token 過期的使用者看到權限錯誤而不是重新登入。
    """
    response = await client.get("/api/v1/knowledge-bases")

    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


async def test_every_knowledge_route_declares_a_scope() -> None:
    """反查：`api/v1/knowledge.py` 的每一條路由都必須出現在上面的矩陣裡。

    逐條列舉「我寫過的端點」永遠測不出「我沒想到的端點」。新增一支端點卻忘了加進
    矩陣時，那支端點的權限就從來沒有被驗證過——而它照樣能跑。
    """
    from api.v1.knowledge import router

    declared = {
        (method, f"/api/v1{route.path}")  # type: ignore[attr-defined]
        for route in router.routes
        for method in getattr(route, "methods", set())
        if method != "HEAD"
    }
    covered = {(method, path) for method, path, _, _ in PERMISSION_MATRIX}

    # 矩陣裡的 {kb} / {doc} 對應 FastAPI 的 {kb_id} / {document_id}，比對前正規化。
    def _normalise(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
        return {
            (method, path.replace("{kb_id}", "{kb}").replace("{document_id}", "{doc}"))
            for method, path in pairs
        }

    missing = _normalise(declared) - _normalise(covered)

    assert not missing, f"以下路由沒有進權限矩陣：{sorted(missing)}"
