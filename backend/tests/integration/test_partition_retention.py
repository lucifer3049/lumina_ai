"""驗收：到期分區的處置（05 §5.2／§7，2A-4 收尾）。

2A-1 起 Beat 只會**建**未來分區，從來不處理過期的——保留政策（usage_logs 13 個月、
audit_logs 依法規 3–7 年）在文件裡，在程式裡沒有對應物。三張分區表因此只增不減。

**預設只 DETACH、不 DROP**（人類拍板）：摘下來的分區不再屬於父表（查詢不掃、
`pg_class` 上仍在），要真的刪除必須另外明示開啟。理由是這是整套維運任務裡唯一
**不可逆**的一步——保留期算錯或設定打錯一個字，損失的是幾個月的真資料，而
DETACH 錯了可以 ATTACH 回去。

**測試對象是臨時的探針表，不是 `platform_usagelog`。** 這不是為了方便：xdist 的
worker **共用同一個測試資料庫**（tests/conftest.py 以租戶 UUID 分割），而
`ALTER TABLE ... DETACH PARTITION` 要拿父表的 ACCESS EXCLUSIVE lock——對真表動手
會把其他 worker 正在跑的每一筆寫入擋在那裡，症狀是**別的檔案**隨機紅（實測：
上傳的孤兒物件檢查與 SSE 續傳各紅過一次，而且兩輪紅的還不一樣）。被測函式對表名
不挑：換掉的是「哪一張表」，換不掉的是 DETACH／DROP 的語意與上界的判讀。真表與
保留期設定的連結由 `TestRetentionConfig` 顧。

三件錯了都不會有例外：

1. **邊界算錯一個月**。多摘一個月＝多刪 30 天的帳；`<=` 寫成 `<` 則永遠少摘一個
   月，而那只會表現為「空間比預期大一點」。
2. **摘到還在用的分區**。查詢不會報錯，只會安靜地少掉最近的資料。
3. **沒設保留期的表被摘**。`messages` 的保留期是「依租戶方案」，而那個機制還不
   存在——沒有設定就必須是「不動」，不是「用某個預設值刪掉」。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from django.db import connections

from services.platform.maintenance import detach_expired_partitions, retention_months

# admin：分區維護走 owner 連線（應用角色沒有 DDL 權限）。
pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "admin"])

# 界線：上界 <= 這一天的分區算過期。年份遠離真實資料，讀起來一眼是測試用的。
CUTOFF = date(1990, 2, 1)
MONTHS = (
    ("1989_12", "1989-12-01", "1990-01-01"),  # 過期
    ("1990_01", "1990-01-01", "1990-02-01"),  # 過期（上界剛好等於界線）
    ("1990_02", "1990-02-01", "1990-03-01"),  # 保留
)


def _sql(statement: str, params: list[str] | None = None) -> list[tuple[object, ...]]:
    with connections["admin"].cursor() as cursor:
        cursor.execute(statement, params)
        return list(cursor.fetchall()) if cursor.description else []


class Probe:
    """一張與真表同形狀的臨時分區表（每次一個新名字，worker 之間不會撞）。"""

    def __init__(self) -> None:
        self.table = f"retention_probe_{uuid.uuid4().hex[:8]}"

    def create(self) -> None:
        _sql(
            f"CREATE TABLE {self.table} (id uuid NOT NULL, created_at timestamptz NOT NULL, "
            f"PRIMARY KEY (id, created_at)) PARTITION BY RANGE (created_at);"
        )
        for suffix, start, end in MONTHS:
            _sql(
                f"CREATE TABLE {self.table}_{suffix} PARTITION OF {self.table} "
                f"FOR VALUES FROM ('{start}') TO ('{end}');"
            )

    def drop(self) -> None:
        # CASCADE 收掉還連著的分區；已被 DETACH 的要自己來——「摘下來還在」正是
        # 預設行為，測試自己也得負責清掉它。
        _sql(f"DROP TABLE IF EXISTS {self.table} CASCADE;")
        for suffix, _, _ in MONTHS:
            _sql(f"DROP TABLE IF EXISTS {self.table}_{suffix};")

    def partition(self, suffix: str) -> str:
        return f"{self.table}_{suffix}"

    def children(self) -> set[str]:
        return {
            str(row[0])
            for row in _sql(
                """
                SELECT c.relname
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE i.inhparent = %s::regclass
                """,
                [self.table],
            )
        }


def _table_exists(name: str) -> bool:
    return bool(_sql("SELECT to_regclass(%s) IS NOT NULL", [name])[0][0])


@pytest.fixture
def probe() -> Iterator[Probe]:
    """**一定要自己收**：DDL 不受 `transaction=True` 的 rollback 保護。"""
    subject = Probe()
    subject.create()
    yield subject
    subject.drop()


@pytest.fixture
def bystander() -> Iterator[Probe]:
    """沒有保留期設定的表——一根都不能被碰到。"""
    subject = Probe()
    subject.create()
    yield subject
    subject.drop()


class TestDetach:
    def test_expired_partitions_leave_the_parent(self, probe: Probe) -> None:
        detached = detach_expired_partitions({probe.table: CUTOFF})

        expired = {probe.partition("1989_12"), probe.partition("1990_01")}
        assert set(detached) == expired
        assert not (expired & probe.children())

    def test_the_data_is_still_there_because_detach_is_not_delete(self, probe: Probe) -> None:
        """預設路徑不刪任何東西：摘下來的是一張普通表，錯了可以 ATTACH 回去。"""
        detach_expired_partitions({probe.table: CUTOFF})

        assert _table_exists(probe.partition("1990_01"))

    def test_the_boundary_month_is_kept(self, probe: Probe) -> None:
        """上界 **等於** 界線的算過期（它涵蓋的全是界線之前的時間），下一個月不算。
        差一個月就是差一個月的帳。"""
        detach_expired_partitions({probe.table: CUTOFF})

        assert probe.partition("1990_02") in probe.children()

    def test_tables_without_a_retention_policy_are_never_touched(
        self, probe: Probe, bystander: Probe
    ) -> None:
        """`messages` 的保留期是「依租戶方案」，那個機制還不存在——**沒有設定
        就是不動**。用某個預設值替它決定，是拿別人的資料賭我猜對了。"""
        detach_expired_partitions({probe.table: CUTOFF})

        assert len(bystander.children()) == len(MONTHS)

    def test_it_is_idempotent(self, probe: Probe) -> None:
        """Beat 每月跑、部署時也可能手動跑。第二次應該無事可做而不是報錯。"""
        detach_expired_partitions({probe.table: CUTOFF})

        assert detach_expired_partitions({probe.table: CUTOFF}) == []

    def test_dropping_is_opt_in(self, probe: Probe) -> None:
        """真的刪除要明示——這是整套維運任務裡唯一不可逆的一步。"""
        detach_expired_partitions({probe.table: CUTOFF}, drop=True)

        assert not _table_exists(probe.partition("1990_01"))


class TestRetentionConfig:
    def test_the_defaults_cover_the_two_tables_with_a_written_policy(self) -> None:
        """05 §7：usage_logs 13 個月、audit_logs 依法規 3–7 年（取上限）。
        `conversation_message` 刻意不在裡面（見上）。"""
        months = retention_months()

        assert months["platform_usagelog"] == 13
        assert months["platform_auditlog"] == 84
        assert "conversation_message" not in months
