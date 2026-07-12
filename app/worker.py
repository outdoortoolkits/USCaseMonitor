import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.services.delivery import deliver_pending
from app.services.polling import poll_all
from app.services.reminders import process_due_time_rules
from app.services.system_config import effective_settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


def scheduled_cycle() -> None:
    with SessionLocal() as db:
        current = effective_settings(db)
        process_due_time_rules(db)
        poll_all(db, current)
        deliver_pending(db, current)


def delivery_cycle() -> None:
    with SessionLocal() as db:
        process_due_time_rules(db)
        deliver_pending(db, effective_settings(db))


def main() -> None:
    init_db()
    scheduler = BlockingScheduler(timezone=settings.app_timezone)
    scheduler.add_job(scheduled_cycle, "interval", hours=settings.poll_interval_hours, id="uscis_poll", max_instances=1, coalesce=True, next_run_time=datetime.now())
    scheduler.add_job(delivery_cycle, "interval", minutes=5, id="email_delivery", max_instances=1, coalesce=True)
    logger.info("Worker started; polling every %s hours", settings.poll_interval_hours)
    scheduler.start()


if __name__ == "__main__":
    main()
