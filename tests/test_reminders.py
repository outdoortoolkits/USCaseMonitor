from datetime import date

from sqlalchemy import select

from app.models import AnchorType, Case, CaseStatusSnapshot, Customer, CustomerRuleLink, NotificationEvent, ReminderRule, RuleType, SystemSetting
from app.services.reminders import process_due_time_rules, process_status_rules, snapshot_hash
from app.services.system_config import config_status, effective_settings, save_settings


def make_case(db):
    customer = Customer(name="测试客户", sign_date=date(2026, 1, 1))
    db.add(customer)
    db.flush()
    case = Case(customer_id=customer.id, form_type="I-129", receipt_number="EAC9999103402", receipt_date=date(2026, 1, 10))
    db.add(case)
    db.flush()
    return case, customer


def test_status_event_is_idempotent(db):
    case, customer = make_case(db)
    rule = ReminderRule(name="状态变化", rule_type=RuleType.STATUS_CHANGED)
    db.add(rule)
    db.flush()
    db.add(CustomerRuleLink(customer_id=customer.id, rule_id=rule.id))
    snapshot = CaseStatusSnapshot(case_id=case.id, status_title="Case Was Approved", status_description="Approved.", status_tag="APPROVED", content_hash=snapshot_hash("Case Was Approved", "Approved."), raw_json="{}", is_changed=True)
    db.add(snapshot)
    db.commit()
    assert process_status_rules(db, case, snapshot) == 1
    assert process_status_rules(db, case, snapshot) == 0
    assert len(db.scalars(select(NotificationEvent)).all()) == 1


def test_relative_time_rule_only_fires_on_due_date(db):
    case, customer = make_case(db)
    rule = ReminderRule(name="半年提醒", rule_type=RuleType.RELATIVE_DATE, anchor_type=AnchorType.SIGN_DATE, days_offset=180)
    db.add(rule)
    db.flush()
    db.add(CustomerRuleLink(customer_id=customer.id, rule_id=rule.id))
    db.commit()
    assert process_due_time_rules(db, date(2026, 6, 30)) == 1
    assert process_due_time_rules(db, date(2026, 6, 30)) == 0


def test_first_snapshot_does_not_create_status_event(db):
    case, customer = make_case(db)
    rule = ReminderRule(name="状态变化", rule_type=RuleType.STATUS_CHANGED)
    db.add(rule)
    db.flush()
    db.add(CustomerRuleLink(customer_id=customer.id, rule_id=rule.id))
    snapshot = CaseStatusSnapshot(case_id=case.id, status_title="Received", status_description="Received.", status_tag="RECEIVED", content_hash="a" * 64, raw_json="{}", is_changed=False)
    db.add(snapshot)
    db.commit()
    assert process_status_rules(db, case, snapshot) == 0


def test_sensitive_system_settings_are_encrypted(db):
    save_settings(db, {"smtp_username": "user@test.com", "smtp_auth_code": "secret-value"})
    current = effective_settings(db)
    assert current.smtp_auth_code == "secret-value"
    assert current.smtp_username == "user@test.com"
    assert config_status(db)["smtp_secret"] is True
    row = db.get(SystemSetting, "smtp_auth_code")
    assert row.value != "secret-value"
