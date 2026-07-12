import json
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Case, CaseStatusSnapshot, PollingRun
from app.services.reminders import process_status_rules, snapshot_hash
from app.services.uscis import USCISClient, classify_status


def poll_case(db: Session, settings: Settings, case: Case, client: USCISClient | None = None) -> CaseStatusSnapshot:
    client = client or USCISClient(settings)
    status = client.get_case_status(case.receipt_number)
    digest = snapshot_hash(status.title, status.description)
    previous = db.scalar(
        select(CaseStatusSnapshot).where(CaseStatusSnapshot.case_id == case.id).order_by(CaseStatusSnapshot.checked_at.desc())
    )
    if previous and previous.content_hash == digest:
        case.last_checked_at = datetime.now(timezone.utc)
        case.last_error = ""
        db.commit()
        return previous
    snapshot = CaseStatusSnapshot(
        case_id=case.id,
        status_title=status.title,
        status_description=status.description,
        status_tag=classify_status(status.title, status.description),
        content_hash=digest,
        source_modified_at=status.modified_at,
        raw_json=json.dumps(status.raw, ensure_ascii=False),
        is_changed=previous is not None,
    )
    case.last_checked_at = datetime.now(timezone.utc)
    case.last_error = ""
    if status.form_type and not case.form_type:
        case.form_type = status.form_type
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    process_status_rules(db, case, snapshot)
    return snapshot


def poll_all(db: Session, settings: Settings, trigger: str = "SCHEDULED") -> PollingRun:
    cases = db.scalars(
        select(Case).options(selectinload(Case.customer)).where(Case.active.is_(True)).limit(settings.poll_batch_size)
    ).all()
    run = PollingRun(trigger=trigger, total=len(cases))
    db.add(run)
    db.commit()
    client = USCISClient(settings)
    try:
        for index, case in enumerate(cases):
            try:
                poll_case(db, settings, case, client)
                run.succeeded += 1
            except Exception as exc:  # continue batch; details retained per case
                db.rollback()
                current = db.get(Case, case.id)
                current.last_checked_at = datetime.now(timezone.utc)
                current.last_error = str(exc)[:1000]
                run.failed += 1
            db.commit()
            if index < len(cases) - 1 and settings.poll_delay_seconds:
                time.sleep(settings.poll_delay_seconds)
    finally:
        client.close()
    run.status = "COMPLETED" if run.failed == 0 else "COMPLETED_WITH_ERRORS"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    return run

