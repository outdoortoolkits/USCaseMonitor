import asyncio
import re
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import get_db, init_db
from app.models import AnchorType, Case, CaseStatusSnapshot, Customer, CustomerRuleLink, NotificationEvent, PollingRun, ReminderRule, RuleType
from app.services.delivery import deliver_pending
from app.services.polling import poll_case
from app.services.system_config import config_status, effective_settings, save_settings




settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=settings.app_env == "production")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})


def redirect(path: str, message: str = "") -> RedirectResponse:
    suffix = f"?message={quote(message)}" if message else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


def parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def parse_int(value: str) -> int | None:
    return int(value) if value.strip() else None


RULE_TYPE_LABELS: dict[str, str] = {
    "ONE_TIME_DATE": "一次性日期",
    "RELATIVE_DATE": "相对日期",
    "RECURRING": "周期提醒",
    "STATUS_CHANGED": "状态变更",
    "STATUS_MATCHED": "状态匹配",
}

ANCHOR_TYPE_LABELS: dict[str, str] = {
    "SIGN_DATE": "签约日期",
    "RECEIPT_DATE": "收据日期",
    "CUSTOM_DATE": "自定义日期",
}


@app.get("/health")
def health():
    return {"status": "ok", "app": settings.app_name}


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == settings.admin_username and password == settings.admin_password:
        request.session["authenticated"] = True
        return redirect("/")
    return templates.TemplateResponse(request, "login.html", {"error": "用户名或密码错误"}, status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), _: None = Depends(require_login)):
    stats = {
        "customers": db.scalar(select(func.count(Customer.id))) or 0,
        "cases": db.scalar(select(func.count(Case.id)).where(Case.active.is_(True))) or 0,
        "pending": db.scalar(select(func.count(NotificationEvent.id)).where(NotificationEvent.status.in_(["PENDING", "RETRY"]))) or 0,
        "errors": db.scalar(select(func.count(Case.id)).where(Case.last_error != "")) or 0,
    }
    snapshots = db.scalars(select(CaseStatusSnapshot).options(selectinload(CaseStatusSnapshot.case).selectinload(Case.customer)).order_by(CaseStatusSnapshot.checked_at.desc()).limit(10)).all()
    runs = db.scalars(select(PollingRun).order_by(PollingRun.started_at.desc()).limit(5)).all()
    return templates.TemplateResponse(request, "dashboard.html", {"stats": stats, "snapshots": snapshots, "runs": runs, "message": request.query_params.get("message", "")})


@app.get("/customers")
def customers(request: Request, q: str = "", db: Session = Depends(get_db), _: None = Depends(require_login)):
    stmt = select(Customer).options(selectinload(Customer.cases)).order_by(Customer.created_at.desc())
    if q:
        stmt = stmt.outerjoin(Customer.cases).where(or_(Customer.name.ilike(f"%{q}%"), Case.receipt_number.ilike(f"%{q}%"))).distinct()
    return templates.TemplateResponse(request, "customers.html", {"customers": db.scalars(stmt).all(), "q": q, "message": request.query_params.get("message", "")})


@app.post("/customers")
def create_customer(name: str = Form(...), phone: str = Form(""), sign_date: str = Form(""), remark: str = Form(""), db: Session = Depends(get_db), _: None = Depends(require_login)):
    db.add(Customer(name=name.strip(), phone=phone.strip(), sign_date=parse_date(sign_date), remark=remark.strip()))
    db.commit()
    return redirect("/customers", "客户已创建")


@app.post("/customers/{customer_id}/delete")
def delete_customer(customer_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404)
    name = customer.name
    db.delete(customer)
    db.commit()
    return redirect("/customers", f"客户「{name}」已删除")


@app.post("/customers/{customer_id}/toggle")
def toggle_customer(customer_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404)
    customer.active = not customer.active
    db.commit()
    return redirect("/customers", f"客户「{customer.name}」已{'启用' if customer.active else '停用'}")


@app.post("/customers/batch-delete")
def batch_delete_customers(customer_ids: str = Form(...), db: Session = Depends(get_db), _: None = Depends(require_login)):
    ids = [int(x.strip()) for x in customer_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return redirect("/customers", "未选择任何客户")
    customers = db.scalars(select(Customer).where(Customer.id.in_(ids))).all()
    for c in customers:
        db.delete(c)
    db.commit()
    return redirect("/customers", f"已删除 {len(ids)} 位客户")


def _run_batch_poll(ids: list[int], settings_obj: Any) -> str:
    """在独立线程中执行批量查询（Playwright Sync API 不能在 asyncio 中调用）。"""
    import sys, threading as _thr, logging as _log_mod
    _log = _log_mod.getLogger("uscase")

    print(f"[_run_batch_poll] Thread: {_thr.current_thread().name}", file=sys.stderr, flush=True)

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        from app.services.uscis import USCISClient

        # 收集所有选中客户的所有活跃案件，排成一个队列逐个查询
        all_cases: list[tuple[int, Case]] = []
        for cid in ids:
            cases = db.scalars(
                select(Case).where(Case.customer_id == cid, Case.active.is_(True))
            ).all()
            for case in cases:
                all_cases.append((cid, case))

        if not all_cases:
            return "选中的客户没有待查询的案件"

        total = len(all_cases)
        updated = 0
        skipped = 0
        failed = 0
        delay = settings_obj.poll_delay_seconds or 10

        client = USCISClient(settings_obj)
        try:
            for idx, (cid, case) in enumerate(all_cases):
                try:
                    snapshot = poll_case(db, settings_obj, case, client)
                    if snapshot.is_changed:
                        updated += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    _log.exception("poll_case failed for case %d", case.id)
                    db.rollback()
                    current = db.get(Case, case.id)
                    current.last_error = str(exc)[:1000]
                    db.commit()
                    failed += 1
                # 每个案件之间间隔 delay 秒（最后一个不等待）
                if idx < total - 1:
                    time.sleep(delay)
        finally:
            client.close()

        parts = [f"查询 {total} 个案件"]
        if updated:
            parts.append(f"状态更新 {updated} 个")
        if skipped:
            parts.append(f"无变化 {skipped} 个")
        if failed:
            parts.append(f"失败 {failed} 个")
        return "，".join(parts)
    finally:
        db.close()


@app.post("/customers/batch-poll")
async def batch_poll_customers(customer_ids: str = Form(...), db: Session = Depends(get_db), _: None = Depends(require_login)):
    ids = [int(x.strip()) for x in customer_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return redirect("/customers", "未选择任何客户")
    settings_obj = effective_settings(db)
    message = await asyncio.to_thread(_run_batch_poll, ids, settings_obj)
    return redirect("/customers", message)


@app.get("/customers/{customer_id}")
def customer_detail(customer_id: int, request: Request, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.scalar(select(Customer).options(selectinload(Customer.cases).selectinload(Case.snapshots)).where(Customer.id == customer_id))
    if not customer:
        raise HTTPException(404)
    # 获取该客户已关联的规则
    linked_rules = db.scalars(
        select(CustomerRuleLink).where(CustomerRuleLink.customer_id == customer_id)
    ).all()
    linked_rule_ids = {link.rule_id for link in linked_rules}
    linked_rule_map: dict[int, CustomerRuleLink] = {link.rule_id: link for link in linked_rules}
    # 所有启用的规则模板
    all_rules = db.scalars(
        select(ReminderRule).where(ReminderRule.enabled.is_(True)).order_by(ReminderRule.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "customer_detail.html", {
        "customer": customer,
        "all_rules": all_rules,
        "linked_rule_ids": linked_rule_ids,
        "linked_rule_map": linked_rule_map,
        "rule_type_labels": RULE_TYPE_LABELS,
        "message": request.query_params.get("message", ""),
    })


@app.post("/customers/{customer_id}/cases")
def create_case(customer_id: int, form_type: str = Form(...), receipt_number: str = Form(...), receipt_date: str = Form(""), db: Session = Depends(get_db), _: None = Depends(require_login)):
    receipt = re.sub(r"[^A-Za-z0-9]", "", receipt_number).upper()
    if not re.fullmatch(r"[A-Z]{3}[0-9]{10}", receipt):
        return redirect(f"/customers/{customer_id}", "收据编号格式应为3个字母加10位数字")
    if db.scalar(select(Case).where(Case.receipt_number == receipt)):
        return redirect(f"/customers/{customer_id}", "该收据编号已存在")
    db.add(Case(customer_id=customer_id, form_type=form_type.strip().upper(), receipt_number=receipt, receipt_date=parse_date(receipt_date)))
    db.commit()
    return redirect(f"/customers/{customer_id}", "案件已添加")


def _run_single_poll(case_id: int, settings_obj: Any) -> str:
    """在独立线程中执行单个案件查询。"""
    import logging as _log_mod
    _log = _log_mod.getLogger("uscase")
    from app.database import SessionLocal
    from app.services.uscis import USCISClient

    db2 = SessionLocal()
    client = USCISClient(settings_obj)
    try:
        case2 = db2.get(Case, case_id)
        try:
            snapshot = poll_case(db2, settings_obj, case2, client)
            return f"查询完成：{snapshot.status_title}"
        except Exception as exc:
            _log.exception("poll_case failed")
            db2.rollback()
            case2 = db2.get(Case, case_id)
            case2.last_error = str(exc)[:1000]
            db2.commit()
            return str(exc)
    finally:
        client.close()
        db2.close()


@app.post("/cases/{case_id}/poll")
async def manual_poll(case_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    case = db.scalar(select(Case).options(selectinload(Case.customer)).where(Case.id == case_id))
    if not case:
        raise HTTPException(404)
    settings_obj = effective_settings(db)
    message = await asyncio.to_thread(_run_single_poll, case_id, settings_obj)
    return redirect(f"/customers/{case.customer_id}", message)


def _run_poll_all(customer_id: int, settings_obj: Any) -> str:
    """在独立线程中执行单客户全部案件查询（Playwright Sync API 不能在 asyncio 中调用）。"""
    import logging as _log_mod
    _log = _log_mod.getLogger("uscase")

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        from app.services.uscis import USCISClient

        cases = db.scalars(
            select(Case).where(Case.customer_id == customer_id, Case.active.is_(True))
        ).all()
        if not cases:
            return "该客户没有待查询的案件"

        ok = skip = fail = 0
        total = len(cases)
        delay = settings_obj.poll_delay_seconds or 10

        client = USCISClient(settings_obj)
        try:
            for i, case in enumerate(cases):
                try:
                    snapshot = poll_case(db, settings_obj, case, client)
                    if snapshot.is_changed:
                        ok += 1
                    else:
                        skip += 1
                except Exception as exc:
                    _log.exception("poll_case failed for case %d", case.id)
                    db.rollback()
                    current = db.get(Case, case.id)
                    current.last_error = str(exc)[:1000]
                    db.commit()
                    fail += 1
                if i < total - 1:
                    time.sleep(delay)
        finally:
            client.close()

        parts = [f"查询 {total} 个案件"]
        if ok > 0:
            parts.append(f"状态更新 {ok} 个")
        if skip > 0:
            parts.append(f"无变化 {skip} 个")
        if fail > 0:
            parts.append(f"失败 {fail} 个")
        return "，".join(parts)
    finally:
        db.close()


@app.post("/customers/{customer_id}/poll-all")
async def manual_poll_all(customer_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404)
    settings_obj = effective_settings(db)
    message = await asyncio.to_thread(_run_poll_all, customer_id, settings_obj)
    return redirect(f"/customers/{customer_id}", message)


@app.post("/customers/{customer_id}/send")
def manual_send_for_customer(customer_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.get(Customer, customer_id)
    if not customer:
        raise HTTPException(404)
    # 发送该客户案件关联的待处理通知
    event_ids = db.scalars(
        select(NotificationEvent.id)
        .join(Case, NotificationEvent.case_id == Case.id)
        .where(Case.customer_id == customer_id, NotificationEvent.status.in_(["PENDING", "RETRY"]))
    ).all()
    if not event_ids:
        return redirect(f"/customers/{customer_id}", "没有待发送的邮件")

    sent, failed = deliver_pending(db, effective_settings(db), event_ids)
    return redirect(f"/customers/{customer_id}", f"邮件发送完成：成功 {sent}，失败 {failed}")


# ══════════════════════════════════════════════
# 提醒规则模板管理
# ══════════════════════════════════════════════

@app.get("/rules")
def rules_list(request: Request, db: Session = Depends(get_db), _: None = Depends(require_login)):
    all_rules = db.scalars(select(ReminderRule).order_by(ReminderRule.created_at.desc())).all()
    return templates.TemplateResponse(request, "rules.html", {
        "rules": all_rules,
        "rule_types": RuleType,
        "anchor_types": AnchorType,
        "rule_type_labels": RULE_TYPE_LABELS,
        "anchor_type_labels": ANCHOR_TYPE_LABELS,
        "message": request.query_params.get("message", ""),
    })


@app.post("/rules")
def create_rule_template(
    name: str = Form(...),
    description: str = Form(""),
    rule_type: RuleType = Form(...),
    anchor_type: str = Form(""),
    days_offset: str = Form(""),
    custom_date: str = Form(""),
    recurrence_days: str = Form(""),
    status_match: str = Form(""),
    subject_template: str = Form(...),
    body_template: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_login),
):
    parsed_anchor = AnchorType(anchor_type) if anchor_type else None
    try:
        parsed_days = parse_int(days_offset)
        parsed_recurrence = parse_int(recurrence_days)
    except ValueError:
        return redirect("/rules", "天数必须是整数")
    if rule_type == RuleType.RECURRING and (not parsed_recurrence or parsed_recurrence < 1):
        return redirect("/rules", "周期提醒必须填写大于0的循环间隔")
    db.add(ReminderRule(
        name=name, description=description, rule_type=rule_type,
        anchor_type=parsed_anchor, days_offset=parsed_days,
        custom_date=parse_date(custom_date), recurrence_days=parsed_recurrence,
        status_match=status_match.upper(),
        subject_template=subject_template, body_template=body_template,
    ))
    db.commit()
    return redirect("/rules", "提醒规则模板已创建")


@app.post("/rules/{rule_id}/edit")
def edit_rule_template(
    rule_id: int,
    name: str = Form(...),
    description: str = Form(""),
    rule_type: RuleType = Form(...),
    anchor_type: str = Form(""),
    days_offset: str = Form(""),
    custom_date: str = Form(""),
    recurrence_days: str = Form(""),
    status_match: str = Form(""),
    subject_template: str = Form(...),
    body_template: str = Form(...),
    db: Session = Depends(get_db),
    _: None = Depends(require_login),
):
    rule = db.get(ReminderRule, rule_id)
    if not rule:
        raise HTTPException(404)
    parsed_anchor = AnchorType(anchor_type) if anchor_type else None
    try:
        parsed_days = parse_int(days_offset)
        parsed_recurrence = parse_int(recurrence_days)
    except ValueError:
        return redirect("/rules", "天数必须是整数")
    if rule_type == RuleType.RECURRING and (not parsed_recurrence or parsed_recurrence < 1):
        return redirect("/rules", "周期提醒必须填写大于0的循环间隔")
    rule.name = name
    rule.description = description
    rule.rule_type = rule_type
    rule.anchor_type = parsed_anchor
    rule.days_offset = parsed_days
    rule.custom_date = parse_date(custom_date)
    rule.recurrence_days = parsed_recurrence
    rule.status_match = status_match.upper()
    rule.subject_template = subject_template
    rule.body_template = body_template
    db.commit()
    return redirect("/rules", "提醒规则模板已更新")


@app.post("/rules/{rule_id}/toggle")
def toggle_rule_template(rule_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    rule = db.get(ReminderRule, rule_id)
    if not rule:
        raise HTTPException(404)
    rule.enabled = not rule.enabled
    db.commit()
    return redirect("/rules", f"规则「{rule.name}」已{'启用' if rule.enabled else '停用'}")


@app.post("/rules/{rule_id}/delete")
def delete_rule_template(rule_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    rule = db.get(ReminderRule, rule_id)
    if not rule:
        raise HTTPException(404)
    name = rule.name
    # 删除关联
    links = db.scalars(select(CustomerRuleLink).where(CustomerRuleLink.rule_id == rule_id)).all()
    for link in links:
        db.delete(link)
    db.delete(rule)
    db.commit()
    return redirect("/rules", f"规则模板「{name}」已删除")


# ══════════════════════════════════════════════
# 客户-规则关联
# ══════════════════════════════════════════════

@app.post("/customers/{customer_id}/rules/{rule_id}/link")
def link_rule_to_customer(customer_id: int, rule_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    customer = db.get(Customer, customer_id)
    rule = db.get(ReminderRule, rule_id)
    if not customer or not rule:
        raise HTTPException(404)
    existing = db.scalar(
        select(CustomerRuleLink).where(
            CustomerRuleLink.customer_id == customer_id,
            CustomerRuleLink.rule_id == rule_id,
        )
    )
    if existing:
        return redirect(f"/customers/{customer_id}", "该规则已关联")
    db.add(CustomerRuleLink(customer_id=customer_id, rule_id=rule_id))
    db.commit()
    return redirect(f"/customers/{customer_id}", f"已关联规则「{rule.name}」")


@app.post("/customers/{customer_id}/rules/{link_id}/unlink")
def unlink_rule_from_customer(customer_id: int, link_id: int, db: Session = Depends(get_db), _: None = Depends(require_login)):
    link = db.get(CustomerRuleLink, link_id)
    if not link or link.customer_id != customer_id:
        raise HTTPException(404)
    db.delete(link)
    db.commit()
    return redirect(f"/customers/{customer_id}", "已移除规则关联")


@app.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db), _: None = Depends(require_login)):
    events = db.scalars(select(NotificationEvent).order_by(NotificationEvent.created_at.desc()).limit(100)).all()
    current = effective_settings(db)
    return templates.TemplateResponse(request, "notifications.html", {"events": events, "recipients": current.notify_emails, "message": request.query_params.get("message", "")})


@app.post("/notifications/send")
def send_pending(db: Session = Depends(get_db), _: None = Depends(require_login)):
    sent, failed = deliver_pending(db, effective_settings(db))
    return redirect("/notifications", f"发送完成：成功 {sent}，失败 {failed}")


@app.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db), _: None = Depends(require_login)):
    current = effective_settings(db)
    return templates.TemplateResponse(request, "settings.html", {"config": current, "status": config_status(db), "message": request.query_params.get("message", "")})


@app.post("/settings")
def update_settings(
    smtp_host: str = Form(...), smtp_port: str = Form("465"), smtp_use_ssl: str = Form("false"),
    smtp_username: str = Form(""), smtp_auth_code: str = Form(""), smtp_from_name: str = Form("USCaseMonitor"),
    default_notify_emails: str = Form(""), db: Session = Depends(get_db), _: None = Depends(require_login),
):
    try:
        port = int(smtp_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return redirect("/settings", "SMTP 端口无效")
    save_settings(db, {
        "smtp_host": smtp_host.strip(), "smtp_port": str(port),
        "smtp_use_ssl": smtp_use_ssl, "smtp_username": smtp_username.strip(), "smtp_auth_code": smtp_auth_code.strip(),
        "smtp_from_name": smtp_from_name.strip(), "default_notify_emails": default_notify_emails.strip(),
    })
    return redirect("/settings", "系统配置已保存，新凭据已加密存储")
