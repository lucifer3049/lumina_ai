"""驗收：稽核註冊表本身——每條寫入型端點都被分類過（04 §8.3、10 §3，2A-4）。

觸發面的決定（開工前人類核可）：**寫入型請求預設全記**，高頻非敏感的走明文豁免
清單。方向是 fail-safe——新端點忘了宣告會被記下來（只是 action 名字比較粗），而
不是安靜地不留紀錄。

**這份清單刻意手寫而不是從 router 推導**（同 `test_permission_matrix.py` 的理由）：
推導的話它就變成「重述程式碼」，對「新端點沒有人想過該不該記」永遠是綠的——
而那正是本檔要擋的事。加一條寫入型路由就會讓下面兩個 assert 之一紅燈。

順序也在這裡釘住：稽核 middleware 必須跑在 `RequestContextMiddleware` **裡面**。
外面的話，它拿到的租戶 contextvar 已經被清乾淨（`clear_current_tenant_id`）——
症狀是每一列稽核都寫不出去，而使用者端一切正常。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

from api.main import create_app
from api.middleware.audit import AUDIT_ACTIONS, AUDIT_EXEMPT, RECORDED_BY_SERVICE, AuditMiddleware
from api.middleware.request_context import RequestContextMiddleware

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# operation_id → (action, resource_type, 取 resource_id 的 path 參數名)
#
# `path_param` 是 None 代表 id 不在 URL 上（建立類的 id 在回應裡、自助類的主體
# 就是呼叫者自己），由 service 經 `core/audit.py` 的 `describe()` 補。
EXPECTED_ACTIONS = {
    # 使用者與權限異動（04 §8.3 的「權限變更」；本階段角色指派尚無端點）
    "users_create": ("user.create", "user", None),
    "users_update": ("user.update", "user", "user_id"),
    "users_update_me": ("user.update", "user", None),
    "users_deactivate": ("user.deactivate", "user", "user_id"),
    "auth_change_password": ("user.change_password", "user", None),
    # session 撤銷屬敏感操作（10 §2 的 Audit 條）
    "auth_logout": ("auth.logout", "session", None),
    # 設定變更（04 §8.3）
    "tenants_update_current": ("tenant.update", "tenant", None),
    # KB／文件（04 §8.3 明列刪除；建立與改設定一併記——「這個 KB 哪來的」
    # 與「誰刪的」是同一次調查的兩半）
    "knowledge_bases_create": ("knowledge_base.create", "knowledge_base", None),
    "knowledge_bases_update": ("knowledge_base.update", "knowledge_base", "kb_id"),
    "knowledge_bases_delete": ("knowledge_base.delete", "knowledge_base", "kb_id"),
    "documents_upload": ("document.upload", "document", None),
    "documents_reingest": ("document.reingest", "document", "document_id"),
    "documents_delete": ("document.delete", "document", "document_id"),
}

# 豁免——**高頻且非管理面**。每一條都要說得出理由，否則它就是一個沒有人想過的洞。
EXPECTED_EXEMPT = {
    # 每 15 分鐘一次的例行輪換。異常的那一種（reuse detected）走 service 層的
    # 撤銷紀錄，不靠請求層。
    "auth_refresh",
    # 對話是使用者自己的日常資料（04 §8.3 的清單裡沒有它），且每一輪問答都是
    # 一次寫入——記進稽核會讓真正的管理操作被淹沒在聊天紀錄裡。
    # （SSE 的 `conversations_stream_message` 不在此：它是 GET，本清單只管寫入型。）
    "conversations_create",
    "conversations_update",
    "conversations_delete",
    "conversations_send_message",
    "conversations_stop_message",
    # POST 是為了帶 body，語意是讀（09 §2.5）。
    "rag_query",
}

# 請求層記不了的：登入沒有 principal，也沒有租戶 contextvar（AuthService 自己
# 進出 `tenant_context`），middleware 拿不到 tenant_id 就寫不出列。
EXPECTED_BY_SERVICE = {"auth_login"}

_ACTION_PATTERN = re.compile(r"^[a-z][a-z_]*\.[a-z][a-z_]*$")


def _api_routes(routes: Iterable[Any]) -> Iterator[Any]:
    """遞迴走訪路由樹。

    FastAPI 0.141 的 `include_router` 會留下 `_IncludedRouter` 包裝物件而不是把
    路由攤平進 `app.routes`（同 tests/api/test_spike_removal.py 的註記），而它的
    路由掛在 `original_router` 底下——只看第一層會得到空集合，而空集合讓下面
    每一條 assert 都自動通過（`_write_operation_ids` 因此自帶非空斷言）。
    """
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", ()
        )
        yield from _api_routes(nested or ())


def _write_operation_ids() -> set[str]:
    found = {
        route.operation_id
        for route in _api_routes(create_app().routes)
        if route.operation_id is not None and bool((route.methods or set()) & WRITE_METHODS)
    }
    assert found, "一條寫入型路由都沒找到——走訪方式失效了，這份守門會變成空轉"
    return found


def test_the_registry_matches_the_hand_written_list() -> None:
    """註冊表 = 本檔的清單。多一條、少一條、動作名改了都會紅。"""
    actual = {op: tuple(spec) for op, spec in AUDIT_ACTIONS.items()}

    assert actual == EXPECTED_ACTIONS


def test_the_exemptions_match_the_hand_written_list() -> None:
    assert set(AUDIT_EXEMPT) == EXPECTED_EXEMPT
    assert set(RECORDED_BY_SERVICE) == EXPECTED_BY_SERVICE


def test_every_write_route_is_classified() -> None:
    """每條寫入型路由都必須落在三類之一：記、豁免、或由 service 自己記。

    新端點進來時這條會紅——那正是要的：讓「這個操作要不要留稽核」變成一個
    必須回答的問題，而不是一個沒有人問過的問題。
    """
    classified = set(AUDIT_ACTIONS) | set(AUDIT_EXEMPT) | set(RECORDED_BY_SERVICE)
    routes = _write_operation_ids()

    assert not (routes - classified), f"這些寫入型端點沒有分類：{sorted(routes - classified)}"
    assert not (classified - routes), f"註冊表列了不存在的端點：{sorted(classified - routes)}"


def test_a_route_is_not_in_two_categories() -> None:
    """同時「要記」又「豁免」的話，實際行為取決於程式的判斷順序——
    而讀清單的人會以為是另一種。"""
    assert not (set(AUDIT_ACTIONS) & set(AUDIT_EXEMPT))
    assert not (set(AUDIT_ACTIONS) & set(RECORDED_BY_SERVICE))
    assert not (set(AUDIT_EXEMPT) & set(RECORDED_BY_SERVICE))


def test_actions_are_named_resource_dot_verb() -> None:
    """05 §3.3 的 `action t`（`resource.verb`）。命名漂掉的代價是稽核查詢
    要背一份特例表——而查稽核的人多半是在出事的當下才第一次查。"""
    bad = [spec.action for spec in AUDIT_ACTIONS.values() if not _ACTION_PATTERN.match(spec.action)]

    assert not bad, f"這些 action 不是 resource.verb 形狀：{bad}"


def test_audit_middleware_runs_inside_the_request_context() -> None:
    """順序：RequestContextMiddleware 在外、AuditMiddleware 在內。

    反過來的話稽核 middleware 讀到的租戶已經被清空、request_id 也已經解綁，
    每一列都寫不出去——而使用者端完全正常，沒有任何症狀。
    """
    # 比名字而不是比類別：Starlette 把 `Middleware.cls` 標成 `_MiddlewareFactory[P]`，
    # 拿具體類別去比在 mypy strict 下不合型別。名字取自類別本身，改名仍然對得上。
    names = [getattr(entry.cls, "__name__", "") for entry in create_app().user_middleware]

    assert RequestContextMiddleware.__name__ in names, "請求 context middleware 不見了"
    assert AuditMiddleware.__name__ in names, "稽核 middleware 沒有掛上"
    # user_middleware 的第一個是最外層（Starlette 由後往前包）。
    assert names.index(RequestContextMiddleware.__name__) < names.index(AuditMiddleware.__name__)
