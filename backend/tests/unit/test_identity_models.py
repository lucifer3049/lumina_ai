"""驗收：Identity Model 的形狀（05 §3.1、§4；CLAUDE.md 鐵則 6）。

本檔只看 Django 的 model metadata，不碰資料庫——這些是**宣告**是否正確的問題，
不是行為問題，跑起 DB 只會讓它慢十倍。

三類斷言，各自對應一種會靜默出錯的情況：

1. **Model 薄**：業務方法一旦長在 model 上，Service 與 Repository 就會被繞過，
   而繞過的那條路徑沒有租戶檢查。
2. **約束與索引**：`UNIQUE(tenant_id, email)` 寫成全域唯一、或複合索引首欄不是
   tenant_id——兩者在小資料量下都測不出來，前者要等第二個租戶註冊同名信箱、
   後者要等資料長到索引失效才會出現。
3. **欄位歸屬**：哪些表有 tenant_id、哪些沒有，決定了哪些表需要 RLS。
"""

from __future__ import annotations

import inspect

from django.db import models

from apps.identity.models import (
    Permission,
    ResourceGrant,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)

# 需要 tenant 欄位的 model（Tenant 自己用 id 當租戶識別，故不在此列）
TENANT_SCOPED_MODELS = (User, Role, UserRole, RolePermission, ResourceGrant)
ALL_IDENTITY_MODELS = (Tenant, User, Role, Permission, UserRole, RolePermission, ResourceGrant)

# model 允許擁有的方法：Django 自己的一大票，加上專案允許的 __str__。
_ALLOWED_METHOD_NAMES = frozenset({"__str__"})


def _own_methods(model: type[models.Model]) -> set[str]:
    """只看**這個類別自己**定義的方法，不含繼承自 models.Model 的。"""
    return {
        name
        for name, member in vars(model).items()
        if inspect.isfunction(member) and name not in _ALLOWED_METHOD_NAMES
    }


def test_models_stay_thin() -> None:
    """model 只有欄位、Meta、``__str__``（鐵則 6）。

    在 model 上長業務方法的問題不是風格：那些方法通常從 ``self`` 出發直接查
    關聯資料，於是完全不經過 Repository 的 tenant filter。一個
    ``user.get_permissions()`` 看起來人畜無害，實際上是一條沒有租戶檢查的資料路徑。
    """
    offenders = {model.__name__: _own_methods(model) for model in ALL_IDENTITY_MODELS}
    offenders = {name: methods for name, methods in offenders.items() if methods}

    assert not offenders, (
        f"model 上出現業務方法：{offenders}——業務規則放 Service、查詢放 Repository（鐵則 6）"
    )


def test_every_tenant_scoped_model_has_a_tenant_column() -> None:
    """凡是含租戶資料的表都要有 tenant_id（05 §2）。

    ``user_roles`` 與 ``role_permissions`` 也在內。05 §3.1 的欄位表只寫了兩個
    外鍵，但少了 tenant_id 就沒有東西可供 RLS 比對——policy 只能靠 join 回
    ``roles`` / ``users`` 去推，那既慢又讓 policy 的正確性依賴另一張表的 policy。
    多一欄冗餘換 policy 條件單純，是這裡刻意的取捨。
    """
    missing = [
        model.__name__
        for model in TENANT_SCOPED_MODELS
        if "tenant_id"
        not in {field.attname for field in model._meta.get_fields() if hasattr(field, "attname")}
    ]

    assert not missing, f"缺 tenant_id 欄位：{missing}"


def test_permission_is_a_global_dictionary() -> None:
    """`Permission` 沒有 tenant，且 ``code`` 全域唯一。

    它存的是 ``knowledge:write`` 這類代碼，內容對所有租戶相同。給它 tenant_id 的話
    每個租戶都要一份複本，而「兩個租戶的 knowledge:write 是不是同一件事」會變成
    要 join 才能回答的問題。
    """
    field_names = {field.name for field in Permission._meta.get_fields()}
    assert "tenant" not in field_names and "tenant_id" not in field_names

    code_field = Permission._meta.get_field("code")
    assert getattr(code_field, "unique", False), "permissions.code 必須全域唯一（05 §3.1）"


def test_user_email_is_unique_within_a_tenant_only() -> None:
    """``UNIQUE(tenant_id, email)``，不是 ``UNIQUE(email)``。

    全域唯一會讓「同一個人在兩家客戶各有帳號」變成不可能——顧問、外包、集團內
    調動都會踩到，而錯誤訊息只會說 email 已被使用，不會說是別的租戶佔用的。
    """
    unique_pairs = {tuple(fields) for fields in User._meta.unique_together}
    constraint_fields = {
        tuple(constraint.fields)
        for constraint in User._meta.constraints
        if isinstance(constraint, models.UniqueConstraint) and constraint.fields
    }

    assert ("tenant", "email") in unique_pairs | constraint_fields or (
        "tenant_id",
        "email",
    ) in unique_pairs | constraint_fields, "缺 UNIQUE(tenant_id, email)"

    email_field = User._meta.get_field("email")
    assert not getattr(email_field, "unique", False), (
        "email 被宣告為全域唯一——跨租戶同信箱是合法情境（見 docstring）"
    )


def test_composite_indexes_lead_with_tenant_id() -> None:
    """複合索引首欄一律 tenant_id（05 §2）。

    首欄不是 tenant_id 的索引，在「先過濾租戶再排序」的查詢裡幾乎用不到——
    PostgreSQL 會退化成掃描 + filter。這在開發環境（每租戶幾十列）完全看不出來，
    要等到租戶數上百、每個租戶只佔總量千分之幾時才會顯現為查詢變慢。
    """
    offenders = []
    for model in TENANT_SCOPED_MODELS:
        for index in model._meta.indexes:
            first = index.fields[0].lstrip("-")
            if len(index.fields) > 1 and first not in ("tenant", "tenant_id"):
                offenders.append(f"{model.__name__}.{index.name}: {index.fields}")

    assert not offenders, f"複合索引首欄不是 tenant_id：{offenders}"


def test_user_has_the_status_lookup_index() -> None:
    """`(tenant_id, status)` —— 05 §4 指名的兩個查詢（登入、列表）之一。"""
    index_fields = {tuple(index.fields) for index in User._meta.indexes}

    assert ("tenant", "status") in index_fields or ("tenant_id", "status") in index_fields, (
        f"users 缺 (tenant_id, status) 索引，現有：{sorted(index_fields)}"
    )


def test_role_tenant_is_nullable_for_system_roles() -> None:
    """`roles.tenant_id` 可為 NULL，代表四個系統內建角色（Owner/Admin/Editor/Viewer）。

    這是整個 identity 唯一允許 NULL 租戶的地方，也因此是 RLS policy 的例外
    （見 tests/integration/test_rls_identity.py）。若哪天有人把它改成 NOT NULL，
    系統角色就得在每個租戶複製一份，而「不可修改的內建角色」這個保證會消失。
    """
    tenant_field = Role._meta.get_field("tenant")

    assert tenant_field.null is True, "roles.tenant 必須可為 NULL（系統角色共用）"
    assert Role._meta.get_field("is_system") is not None
