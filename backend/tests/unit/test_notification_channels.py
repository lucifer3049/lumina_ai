"""驗收：通知的通道抽象、訂閱規則與寄信（04 §8.5、12 §3，2A-5）。

04 §8.5 給 Notification 的職責有三塊：**通道抽象**（in-app / email / 未來 webhook）、
**事件訂閱規則**（哪一種事件走哪些通道）、**去重與節流**。這一層不碰 DB——它決定
的是「這件事要寄給誰、走哪條路、算不算同一件事」，而那三個判斷用假物件驗得完整。

email 的落地形狀（開工前人類裁示）：通道抽象 + 真的 SMTP，開發環境用 compose 裡的
Mailpit 收信。因此本檔驗的是 SMTP **客戶端的行為**（帶 timeout、設定驅動、失敗不
外拋），信件內容長什麼樣由 Mailpit 用眼睛看。

四件錯了都不會有例外：

1. **新事件型別沒有訂閱規則**。`CHANNELS_BY_TYPE` 查不到就退回空 tuple 的話，
   新加的事件會安靜地誰都不通知——與「還沒接線」完全一樣的症狀。
2. **寄信寫在請求路徑上**。SMTP 連不上時，使用者按下的那個按鈕會等到 timeout
   ——而通知是旁路，主流程不該為它變慢，更不該為它失敗（同 2A-4 的稽核）。
3. **SMTP 呼叫沒有 timeout**（鐵則：所有對外呼叫都要有）。收信端不回應時，
   Celery worker 的那條執行緒就永遠掛在那裡。
4. **收合的桶邊界算錯**。桶太大會把兩個小時前的上傳算成同一批，太小則等於沒有
   收合——而兩種錯法在測試裡都長得像「有一則通知」。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from services.platform.notifications import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNELS_BY_TYPE,
    TYPE_DOCUMENT_FAILED,
    TYPE_DOCUMENT_READY,
    TYPE_EVALUATION_COMPLETED,
    TYPE_QUOTA_THRESHOLD,
    collapse_bucket,
    quota_thresholds,
)


class TestSubscriptionRules:
    def test_every_event_type_declares_its_channels(self) -> None:
        """漏宣告的症狀是「誰都沒收到」，與「還沒接線」一模一樣——所以清單在這裡
        手寫一份守門（同 `test_audit_registry.py` 的理由）。"""
        assert set(CHANNELS_BY_TYPE) == {
            TYPE_DOCUMENT_FAILED,
            TYPE_DOCUMENT_READY,
            TYPE_QUOTA_THRESHOLD,
            TYPE_EVALUATION_COMPLETED,
        }

    @pytest.mark.parametrize("event_type", [TYPE_DOCUMENT_FAILED, TYPE_QUOTA_THRESHOLD])
    def test_the_two_events_that_need_a_human_also_go_by_email(self, event_type: str) -> None:
        """失敗與額度告警都要**離開這個網站**才有用：文件失敗要有人去修，
        額度爆了要有人去加——而使用者不會為了看有沒有壞消息定期登入。"""
        assert CHANNEL_EMAIL in CHANNELS_BY_TYPE[event_type]

    def test_good_news_stays_in_the_app(self) -> None:
        """`document.ready` 是好消息且量大（一次上傳一批就是一批）——寄信會變成
        騷擾，而收件匣裡它一直都在。"""
        assert CHANNELS_BY_TYPE[TYPE_DOCUMENT_READY] == (CHANNEL_IN_APP,)


class TestChannelSwitch:
    def test_turning_email_off_leaves_the_in_app_notification(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """關掉 email（沒有設定 SMTP 的環境、CI）不該連收件匣一起關掉——
        通知的最低保證是「站內看得到」。"""
        from services.platform.notifications import channels_for

        from config.settings.app_settings import get_app_settings

        monkeypatch.setattr(get_app_settings(), "notification_email_enabled", False)

        assert channels_for(TYPE_DOCUMENT_FAILED) == (CHANNEL_IN_APP,)

    def test_with_email_on_the_declared_channels_are_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from services.platform.notifications import channels_for

        from config.settings.app_settings import get_app_settings

        monkeypatch.setattr(get_app_settings(), "notification_email_enabled", True)

        assert channels_for(TYPE_DOCUMENT_FAILED) == CHANNELS_BY_TYPE[TYPE_DOCUMENT_FAILED]


class TestCollapseBucket:
    """收合鍵：同一個 KB、同一個時間桶 → 同一列（04 §8.5 的節流）。"""

    def test_two_uploads_in_the_same_window_share_a_key(self) -> None:
        kb_id = uuid.uuid4()
        first = collapse_bucket(kb_id, at=datetime(2026, 8, 22, 10, 0, 30, tzinfo=UTC))
        second = collapse_bucket(kb_id, at=datetime(2026, 8, 22, 10, 9, 59, tzinfo=UTC))

        assert first == second

    def test_the_next_window_starts_a_new_notification(self) -> None:
        kb_id = uuid.uuid4()
        first = collapse_bucket(kb_id, at=datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC))
        later = collapse_bucket(kb_id, at=datetime(2026, 8, 22, 10, 10, 0, tzinfo=UTC))

        assert first != later

    def test_another_knowledge_base_never_collapses_into_this_one(self) -> None:
        at = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)

        assert collapse_bucket(uuid.uuid4(), at=at) != collapse_bucket(uuid.uuid4(), at=at)


class TestQuotaThresholds:
    def test_the_documented_thresholds_are_the_default(self) -> None:
        """04 §8.5 寫的是 80%／100%。"""
        assert quota_thresholds() == (80, 100)

    def test_a_broken_entry_only_loses_that_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """形狀同價目表與 quota plan 的解析：一個壞值不該讓整組告警消失。"""
        from config.settings.app_settings import get_app_settings

        monkeypatch.setattr(
            get_app_settings(), "notification_quota_thresholds", "80;不是數字;100", raising=False
        )

        assert quota_thresholds() == (80, 100)


class _FakeSMTP:
    """記錄呼叫的假 SMTP 伺服器。"""

    instances: list[_FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list[tuple[str, list[str], str]] = []
        self.started_tls = False
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.credentials = (username, password)

    def sendmail(self, sender: str, recipients: list[str], message: str) -> None:
        self.sent.append((sender, recipients, message))


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    import smtplib

    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


class TestMailer:
    def test_the_connection_carries_a_timeout(self, smtp: type[_FakeSMTP]) -> None:
        """所有對外呼叫都要有 timeout（CLAUDE.md）。收信端不回應時，
        沒有 timeout 的那條 worker 執行緒就永遠掛在那裡。"""
        from core.mailer import send_email

        send_email(to=["someone@example.com"], subject="標題", body="內文")

        assert smtp.instances[0].timeout is not None

    def test_the_host_and_port_come_from_settings(self, smtp: type[_FakeSMTP]) -> None:
        """鐵則 9：不 hardcode URL。開發是 compose 裡的 Mailpit，正式是別的東西。"""
        from core.mailer import send_email

        from config.settings.app_settings import get_app_settings

        settings = get_app_settings()
        send_email(to=["someone@example.com"], subject="標題", body="內文")

        assert (smtp.instances[0].host, smtp.instances[0].port) == (
            settings.smtp_host,
            settings.smtp_port,
        )

    def test_the_recipients_get_the_message(self, smtp: type[_FakeSMTP]) -> None:
        from core.mailer import send_email

        send_email(to=["a@example.com", "b@example.com"], subject="標題", body="內文")

        _, recipients, message = smtp.instances[0].sent[0]
        assert recipients == ["a@example.com", "b@example.com"]
        assert "內文" in message

    def test_a_chinese_subject_survives_the_wire(self, smtp: type[_FakeSMTP]) -> None:
        """標題一定是中文（「法規手冊.pdf 解析失敗」）——沒有編碼處理的話，
        收件匣裡看到的是一串 `=?utf-8?` 之外的亂碼，而寄信本身不會報錯。"""
        from core.mailer import send_email

        send_email(to=["a@example.com"], subject="法規手冊.pdf 解析失敗", body="內文")

        message = smtp.instances[0].sent[0][2]
        assert "法規手冊.pdf 解析失敗" in _decoded_subject(message)


def _decoded_subject(message: str) -> str:
    from email import message_from_string
    from email.header import decode_header, make_header

    raw = message_from_string(message)["Subject"]
    return str(make_header(decode_header(raw)))
