import logging

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_preset_rules() -> None:
    """首次启动时插入预置提醒规则模板"""
    from sqlalchemy import select

    from app.models import AnchorType, ReminderRule, RuleType

    db = SessionLocal()
    try:
        existing = db.scalars(select(ReminderRule).limit(1)).first()
        if existing is not None:
            return  # 已有规则，跳过预置

        presets = [
            ReminderRule(
                name="案件状态变更通知",
                description="当案件状态发生任何变化时发送通知",
                rule_type=RuleType.STATUS_CHANGED,
                subject_template="[{{ status_tag }}] {{ name }} - {{ form_type }} 状态已更新",
                body_template="{{ name }} 的 {{ form_type }}（{{ receipt_number }}）状态已更新。\n\n新的状态：{{ status_title }}\n\n详情：{{ status_description }}",
                enabled=True,
            ),
            ReminderRule(
                name="案件批准通知",
                description="当案件状态被批准（APPROVED）时发送通知",
                rule_type=RuleType.STATUS_MATCHED,
                status_match="APPROVED",
                subject_template="[已批准] {{ name }} - {{ form_type }} 获批",
                body_template="恭喜！{{ name }} 的 {{ form_type }}（{{ receipt_number }}）已获批。\n\n详情：{{ status_description }}",
                enabled=True,
            ),
            ReminderRule(
                name="补件通知（RFE）",
                description="当案件收到补件要求（RFE）时发送通知",
                rule_type=RuleType.STATUS_MATCHED,
                status_match="RFE",
                subject_template="[补件通知] {{ name }} - {{ form_type }} 需要补件",
                body_template="注意！{{ name }} 的 {{ form_type }}（{{ receipt_number }}）收到了补件要求，请尽快处理。\n\n详情：{{ status_description }}",
                enabled=True,
            ),
            ReminderRule(
                name="案件拒绝通知",
                description="当案件被拒绝（DENIED）时发送通知",
                rule_type=RuleType.STATUS_MATCHED,
                status_match="DENIED",
                subject_template="[拒绝通知] {{ name }} - {{ form_type }} 被拒绝",
                body_template="注意！{{ name }} 的 {{ form_type }}（{{ receipt_number }}）已被拒绝。\n\n详情：{{ status_description }}",
                enabled=True,
            ),
            ReminderRule(
                name="收据日期后 90 天提醒",
                description="收据日期后 90 天发送提醒，用于定期跟进",
                rule_type=RuleType.RELATIVE_DATE,
                anchor_type=AnchorType.RECEIPT_DATE,
                days_offset=90,
                subject_template="[跟进提醒] {{ name }} - {{ form_type }}",
                body_template="{{ name }} 的 {{ form_type }}（{{ receipt_number }}）已提交 90 天，请关注案件进展。\n\n收据日期：{{ receipt_date }}",
                enabled=True,
            ),
            ReminderRule(
                name="签约日期后 180 天提醒",
                description="签约日期后 180 天发送提醒",
                rule_type=RuleType.RELATIVE_DATE,
                anchor_type=AnchorType.SIGN_DATE,
                days_offset=180,
                subject_template="[周期跟进] {{ name }} - {{ form_type }} 签约已 180 天",
                body_template="{{ name }} 的 {{ form_type }}（{{ receipt_number }}）自签约已过去 180 天。\n\n签约日期：{{ sign_date }}\n当前状态：{{ status_title }}",
                enabled=True,
            ),
            ReminderRule(
                name="每 30 天定期检查",
                description="每 30 天自动提醒检查案件状态",
                rule_type=RuleType.RECURRING,
                recurrence_days=30,
                subject_template="[定期检查] {{ name }} - {{ form_type }}",
                body_template="请检查 {{ name }} 的 {{ form_type }}（{{ receipt_number }}）案件状态。\n\n当前状态：{{ status_title }}\n状态详情：{{ status_description }}",
                enabled=True,
            ),
        ]

        for rule in presets:
            db.add(rule)
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # 预置默认提醒规则模板
    seed_preset_rules()

    # SQLite 兼容迁移
    if settings.database_url.startswith("sqlite"):
        with engine.connect() as conn:
            # 为已有数据库添加 active 列（兼容旧版本）
            try:
                conn.execute(text("ALTER TABLE customers ADD COLUMN active BOOLEAN DEFAULT 1"))
                conn.commit()
            except OperationalError:
                pass

            # 为 reminder_rules 添加 description 列
            try:
                conn.execute(text("ALTER TABLE reminder_rules ADD COLUMN description TEXT DEFAULT ''"))
                conn.commit()
            except OperationalError:
                pass

            # 迁移 reminder_rules：移除 case_id（如有）
            # SQLite 不支持 DROP COLUMN，需重建表
            try:
                result = conn.execute(text("PRAGMA table_info(reminder_rules)"))
                columns = [row[1] for row in result.fetchall()]
                if "case_id" in columns:
                    logging.getLogger("uscase").info("Migrating reminder_rules: removing case_id")
                    conn.execute(text("""
                        CREATE TABLE reminder_rules_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name VARCHAR(120) NOT NULL,
                            description TEXT DEFAULT '',
                            rule_type VARCHAR(50) NOT NULL,
                            anchor_type VARCHAR(50),
                            days_offset INTEGER,
                            custom_date DATE,
                            recurrence_days INTEGER,
                            status_match VARCHAR(80) DEFAULT '',
                            subject_template VARCHAR(500) DEFAULT '[{{status_tag}}] {{name}} - {{form_type}}',
                            body_template TEXT DEFAULT '{{name}} 的 {{form_type}}（{{receipt_number}}）需要处理。\n\n{{status_title}}\n{{status_description}}',
                            enabled BOOLEAN DEFAULT 1,
                            created_at DATETIME
                        )
                    """))
                    conn.execute(text("""
                        INSERT INTO reminder_rules_new (id, name, description, rule_type, anchor_type,
                            days_offset, custom_date, recurrence_days, status_match,
                            subject_template, body_template, enabled, created_at)
                        SELECT id, name, COALESCE(description, ''), rule_type, anchor_type,
                            days_offset, custom_date, recurrence_days, COALESCE(status_match, ''),
                            subject_template, body_template, enabled, created_at
                        FROM reminder_rules
                    """))
                    conn.execute(text("DROP TABLE reminder_rules"))
                    conn.execute(text("ALTER TABLE reminder_rules_new RENAME TO reminder_rules"))
                    conn.commit()
                    logging.getLogger("uscase").info("reminder_rules migration complete")
            except OperationalError:
                pass

