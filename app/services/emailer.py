import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from app.config import Settings


class EmailSendError(RuntimeError):
    pass


def send_email(settings: Settings, recipients: list[str], subject: str, body: str) -> None:
    if not recipients:
        raise EmailSendError("未配置企业通知邮箱")
    if not settings.smtp_username or not settings.smtp_auth_code:
        raise EmailSendError("SMTP 账号或授权码尚未配置")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_username))
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    try:
        if settings.smtp_use_ssl:
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
            smtp.starttls()
        with smtp:
            smtp.login(settings.smtp_username, settings.smtp_auth_code)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailSendError(f"SMTP 发送失败：{exc}") from exc

