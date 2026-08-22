"""SMTP 寄信 —— 每一個外部系統在 repo 內只有一個入口（同 `core/redis.py`、
`core/object_storage.py`）。

**不用 Django 的 mail 框架**：Django 在本專案的角色是 ORM + Migration + Admin
（CLAUDE.md 的技術棧宣告），寄信走它就等於再把 Django settings 拉進另一條路徑，
而收益只有「少寫二十行」。這裡直接用 stdlib 的 `smtplib` + `email.message`。

開發環境的收信端是 compose 裡的 **Mailpit**（信不會真的寄出去，開 8025 埠看得到）。
正式環境換 `SMTP_*` 環境變數即可——鐵則 9：host／port／寄件人都不寫死。

**呼叫端只會是 Celery task**（`worker/notification_tasks.py`）：寄信不在請求路徑上。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from config.logging import get_logger
from config.settings.app_settings import get_app_settings

logger = get_logger(__name__)

__all__ = ["send_email"]


def send_email(*, to: list[str], subject: str, body: str) -> None:
    """寄一封純文字信。連不上／被拒一律往外拋（由 task 決定要不要重試）。

    收件人為空時直接回：沒有人可以寄不是錯誤（例如租戶裡的 owner 全被停用），
    而 `sendmail` 對空清單的行為是拋例外，那會在 DLQ 留下一筆看起來像故障的紀錄。

    標題先過 `_single_line`：通知標題帶檔名（2A-5），而檔名是使用者可控的。
    """
    if not to:
        return

    settings = get_app_settings()
    message = EmailMessage()
    message["From"] = settings.notification_email_from
    message["To"] = ", ".join(to)
    # `EmailMessage` 會自動對非 ASCII 的標題做 RFC 2047 編碼——直接塞中文而不編碼
    # 的話，收件匣裡看到的是亂碼，而寄信本身完全成功（沒有任何錯誤可查）。
    message["Subject"] = _single_line(subject)
    message.set_content(body)

    # timeout 一定要給（CLAUDE.md：所有對外呼叫）。收信端不回應時，沒有它的那條
    # worker 執行緒會一直掛著——症狀是「寄信的 worker 慢慢變成沒有」。
    # `timeout` 一定要用具名參數傳：`SMTP` 的第三個位置參數是 `local_hostname`，
    # 位置傳進去會變成把秒數當主機名，而連線照樣成立（沒有 timeout）。
    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    ) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.sendmail(settings.notification_email_from, to, message.as_string())

    logger.info("notification_email_sent", recipients=len(to))


def _single_line(value: str) -> str:
    r"""把 CR／LF 換成空白——header 只能是一行。

    Python 的 `EmailMessage` 已經擋掉 header injection（含換行的值會拋
    ``ValueError: Header values may not contain linefeed or carriage return
    characters``），所以**這不是安全修補**，是可用性修補：通知標題帶檔名（2A-5），
    而檔名是使用者可控的。一個叫做 ``報告\n.pdf`` 的檔案會讓這則通知的 email
    派送每次都炸、重試三次後進 DLQ，而錯誤訊息半個字都不提檔名。

    換成空白而不是刪掉：使用者仍認得出那是哪一份檔案。``\r\n`` 會變成兩個空白，
    那不值得為它多寫一個正規表示式——標題本來就不保證等寬。
    """
    return value.replace("\r", " ").replace("\n", " ")
