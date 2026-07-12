from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import NotificationEvent
from app.services.emailer import send_email


def deliver_pending(db: Session, settings: Settings, event_ids: list[int] | None = None) -> tuple[int, int]:
    if event_ids:
        events = db.scalars(
            select(NotificationEvent)
            .where(NotificationEvent.id.in_(event_ids), NotificationEvent.attempts < 3)
        ).all()
    else:
        events = db.scalars(
            select(NotificationEvent)
            .where(NotificationEvent.status.in_(["PENDING", "RETRY"]), NotificationEvent.attempts < 3)
            .order_by(NotificationEvent.created_at)
            .limit(50)
        ).all()
    sent = failed = 0
    for event in events:
        event.attempts += 1
        try:
            send_email(settings, settings.notify_emails, event.subject, event.body)
            event.status = "SENT"
            event.sent_at = datetime.now(timezone.utc)
            event.error = ""
            sent += 1
        except Exception as exc:
            event.status = "RETRY" if event.attempts < 3 else "FAILED"
            event.error = str(exc)[:1000]
            failed += 1
        db.commit()
    return sent, failed

