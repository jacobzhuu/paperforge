from __future__ import annotations

import asyncio
import hashlib
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode

from observability import get_logger

from paperforge_api.config import Settings

logger = get_logger(__name__)


async def send_auth_email(
    settings: Settings,
    *,
    recipient: str,
    purpose: str,
    token: str,
) -> None:
    route = "/verify-email" if purpose == "verify_email" else "/reset-password"
    link = f"{settings.public_app_url.rstrip('/')}{route}?{urlencode({'token': token})}"
    subject = "验证 PaperForge 邮箱" if purpose == "verify_email" else "重置 PaperForge 密码"
    body = (
        f"请打开下面的链接完成操作：\n\n{link}\n\n"
        "如果不是你发起的请求，可以忽略这封邮件。"
    )
    if settings.auth_email_mode in {"file", "console"}:
        await asyncio.to_thread(
            _write_development_email,
            settings,
            recipient=recipient,
            purpose=purpose,
            body=body,
            token=token,
        )
        return
    if settings.auth_email_mode != "smtp":
        raise RuntimeError("unsupported auth email mode")
    if not settings.smtp_host or not settings.smtp_from_email:
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = recipient
    message.set_content(body)
    await asyncio.to_thread(_send_smtp, settings, message)


def _write_development_email(
    settings: Settings,
    *,
    recipient: str,
    purpose: str,
    body: str,
    token: str,
) -> None:
    """Write local-only mail without placing reusable tokens in application logs."""
    outbox = Path(settings.auth_email_outbox_dir).resolve()
    outbox.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    path = outbox / f"{purpose}-{digest}.txt"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"To: {recipient}\nPurpose: {purpose}\n\n{body}")
    logger.info(
        "development auth email saved",
        extra={"recipient": recipient, "purpose": purpose, "outbox_file": str(path)},
    )


def _send_smtp(settings: Settings, message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        if settings.smtp_use_tls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)
