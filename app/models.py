import enum
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuleType(str, enum.Enum):
    ONE_TIME_DATE = "ONE_TIME_DATE"
    RELATIVE_DATE = "RELATIVE_DATE"
    RECURRING = "RECURRING"
    STATUS_CHANGED = "STATUS_CHANGED"
    STATUS_MATCHED = "STATUS_MATCHED"


class AnchorType(str, enum.Enum):
    SIGN_DATE = "SIGN_DATE"
    RECEIPT_DATE = "RECEIPT_DATE"
    CUSTOM_DATE = "CUSTOM_DATE"


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    phone: Mapped[str] = mapped_column(String(50), default="")
    sign_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cases: Mapped[list["Case"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class Case(Base):
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    form_type: Mapped[str] = mapped_column(String(40))
    receipt_number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    receipt_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    customer: Mapped[Customer] = relationship(back_populates="cases")
    snapshots: Mapped[list["CaseStatusSnapshot"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class CaseStatusSnapshot(Base):
    __tablename__ = "case_status_snapshots"
    __table_args__ = (UniqueConstraint("case_id", "content_hash", name="uq_case_snapshot_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    status_title: Mapped[str] = mapped_column(String(500))
    status_description: Mapped[str] = mapped_column(Text)
    status_tag: Mapped[str] = mapped_column(String(40), default="OTHER")
    content_hash: Mapped[str] = mapped_column(String(64))
    source_modified_at: Mapped[str] = mapped_column(String(80), default="")
    raw_json: Mapped[str] = mapped_column(Text)
    is_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    case: Mapped[Case] = relationship(back_populates="snapshots")


class ReminderRule(Base):
    """全局提醒规则模板 — 不绑定具体案件，用户可选择启用"""
    __tablename__ = "reminder_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    rule_type: Mapped[RuleType] = mapped_column(Enum(RuleType))
    anchor_type: Mapped[AnchorType | None] = mapped_column(Enum(AnchorType), nullable=True)
    days_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recurrence_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status_match: Mapped[str] = mapped_column(String(80), default="")
    subject_template: Mapped[str] = mapped_column(String(500), default="[{{status_tag}}] {{name}} - {{form_type}}")
    body_template: Mapped[str] = mapped_column(Text, default="{{name}} 的 {{form_type}}（{{receipt_number}}）需要处理。\n\n{{status_title}}\n{{status_description}}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CustomerRuleLink(Base):
    """客户-提醒规则关联表"""
    __tablename__ = "customer_rule_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("reminder_rules.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("case_status_snapshots.id", ondelete="SET NULL"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PollingRun(Base):
    __tablename__ = "polling_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(30), default="SCHEDULED")
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
