import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone

from jinja2 import Template
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnchorType, Case, CaseStatusSnapshot, CustomerRuleLink, NotificationEvent, ReminderRule, RuleType


def render(template: str, context: dict) -> str:
    return Template(template).render(**context)


def event_context(case: Case, snapshot: CaseStatusSnapshot | None = None) -> dict:
    return {
        "name": case.customer.name,
        "form_type": case.form_type,
        "receipt_number": case.receipt_number,
        "status_title": snapshot.status_title if snapshot else "时间节点提醒",
        "status_description": snapshot.status_description if snapshot else "请按计划跟进该客户案件。",
        "status_tag": snapshot.status_tag if snapshot else "REMINDER",
        "receipt_date": case.receipt_date or "",
        "sign_date": case.customer.sign_date or "",
    }


def create_event(db: Session, rule: ReminderRule, case: Case, dedupe_key: str, snapshot: CaseStatusSnapshot | None = None, scheduled_for: datetime | None = None) -> bool:
    context = event_context(case, snapshot)
    event = NotificationEvent(
        rule_id=rule.id,
        case_id=case.id,
        snapshot_id=snapshot.id if snapshot else None,
        dedupe_key=dedupe_key,
        scheduled_for=scheduled_for,
        subject=render(rule.subject_template, context),
        body=render(rule.body_template, context),
    )
    db.add(event)
    try:
        db.flush()
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _get_customer_rules(db: Session, customer_id: int) -> list[ReminderRule]:
    """获取某个客户关联的所有启用的提醒规则"""
    return db.scalars(
        select(ReminderRule)
        .join(CustomerRuleLink, CustomerRuleLink.rule_id == ReminderRule.id)
        .where(CustomerRuleLink.customer_id == customer_id, ReminderRule.enabled.is_(True))
    ).all()


def process_status_rules(db: Session, case: Case, snapshot: CaseStatusSnapshot) -> int:
    if not snapshot.is_changed:
        return 0
    rules = _get_customer_rules(db, case.customer_id)
    created = 0
    for rule in rules:
        if rule.rule_type not in (RuleType.STATUS_CHANGED, RuleType.STATUS_MATCHED):
            continue
        if rule.rule_type == RuleType.STATUS_MATCHED and rule.status_match.upper() != snapshot.status_tag.upper():
            continue
        key = f"status:{rule.id}:{snapshot.id}"
        created += int(create_event(db, rule, case, key, snapshot=snapshot))
    return created


def _anchor_date(rule: ReminderRule, case: Case) -> date | None:
    if rule.anchor_type == AnchorType.SIGN_DATE:
        return case.customer.sign_date
    if rule.anchor_type == AnchorType.RECEIPT_DATE:
        return case.receipt_date
    if rule.anchor_type == AnchorType.CUSTOM_DATE:
        return rule.custom_date
    return None


def process_due_time_rules(db: Session, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    # 获取所有启用的时间类规则模板
    time_rules = db.scalars(
        select(ReminderRule).where(
            ReminderRule.enabled.is_(True),
            ReminderRule.rule_type.in_([RuleType.ONE_TIME_DATE, RuleType.RELATIVE_DATE, RuleType.RECURRING]),
        )
    ).all()
    created = 0
    for rule in time_rules:
        # 找到关联此规则的所有客户的案件
        links = db.scalars(
            select(CustomerRuleLink).where(CustomerRuleLink.rule_id == rule.id)
        ).all()
        for link in links:
            cases = db.scalars(
                select(Case).where(Case.customer_id == link.customer_id, Case.active.is_(True))
            ).all()
            for case in cases:
                anchor = _anchor_date(rule, case)
                if not anchor:
                    continue
                due = anchor + timedelta(days=rule.days_offset or 0)
                if today < due:
                    continue
                occurrence = due
                if rule.rule_type == RuleType.RECURRING:
                    every = rule.recurrence_days or 1
                    elapsed = (today - due).days
                    if elapsed % every != 0:
                        continue
                    occurrence = today
                elif today != due:
                    continue
                scheduled = datetime.combine(occurrence, time.min, tzinfo=timezone.utc)
                key = f"time:{rule.id}:{case.id}:{occurrence.isoformat()}"
                created += int(create_event(db, rule, case, key, scheduled_for=scheduled))
    return created


def snapshot_hash(title: str, description: str) -> str:
    normalized = json.dumps([" ".join(title.split()), " ".join(description.split())], ensure_ascii=False)
    return hashlib.sha256(normalized.encode()).hexdigest()

