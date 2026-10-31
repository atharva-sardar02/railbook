"""Twelve-Factor XII: one-off admin process, same image and config as the API.

python -m app.admin setup   # migrate the schema + seed data (safe to run repeatedly)
"""

import sys
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.log_config import configure_logging
from app.models import Inventory, Schedule, Station, Train

log = structlog.get_logger("railbook.admin")

MIGRATIONS = Path(__file__).parent / "migrations"
# The schema that create_all() built before Alembic was added (= migration 0001).
BASELINE = "0001"

STATIONS = [
    ("NDLS", "New Delhi"),
    ("BCT", "Mumbai Central"),
    ("MAS", "Chennai Central"),
    ("HWH", "Howrah Junction"),
    ("SBC", "KSR Bengaluru"),
]

TRAINS = [
    ("12951", "Mumbai Rajdhani", "BCT", "NDLS", time(17, 0), time(8, 30)),
    ("12952", "New Delhi Rajdhani", "NDLS", "BCT", time(16, 55), time(8, 35)),
    ("12621", "Tamil Nadu Express", "NDLS", "MAS", time(22, 30), time(7, 10)),
    ("12301", "Howrah Rajdhani", "HWH", "NDLS", time(16, 50), time(10, 0)),
    ("12627", "Karnataka Express", "NDLS", "SBC", time(20, 20), time(13, 40)),
    ("12953", "August Kranti Rajdhani", "BCT", "NDLS", time(17, 10), time(10, 55)),
]

# class code -> (seats per schedule, fare)
CLASSES = {
    "SL": (72, Decimal("650.00")),
    "3A": (64, Decimal("1750.00")),
    "2A": (46, Decimal("2450.00")),
}

DAYS_AHEAD = 14


def alembic_config(connection=None) -> Config:
    cfg = Config()  # no alembic.ini: the image doesn't need one
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = connection
    return cfg


def migrate(engine: Engine) -> None:
    """Bring the schema to the newest migration. Alembic records the applied revision in the
    alembic_version table, so each migration runs once per database."""
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        tables = inspect(conn).get_table_names()
        if "alembic_version" not in tables and "bookings" in tables:
            # A database built by create_all() before Alembic: the tables of 0001 already
            # exist. Record 0001 as applied instead of running it (it would fail on CREATE TABLE).
            log.info("migrate_baseline", revision=BASELINE)
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")
    log.info("migrate_complete")


def setup(session: Session) -> None:
    migrate(session.get_bind())

    # INSERT ... ON CONFLICT DO NOTHING keeps this idempotent.
    session.execute(
        insert(Station)
        .values([{"code": c, "name": n} for c, n in STATIONS])
        .on_conflict_do_nothing()
    )
    session.execute(
        insert(Train)
        .values(
            [
                {
                    "number": num,
                    "name": name,
                    "from_station": src,
                    "to_station": dst,
                    "departs_at": dep,
                    "arrives_at": arr,
                }
                for num, name, src, dst, dep, arr in TRAINS
            ]
        )
        .on_conflict_do_nothing()
    )

    start = date.today() - timedelta(days=1)  # containers run in UTC; cover local "today" too
    for offset in range(DAYS_AHEAD):
        travel_date = start + timedelta(days=offset)
        for train_number, *_ in TRAINS:
            schedule_id = session.scalar(
                insert(Schedule)
                .values(train_number=train_number, travel_date=travel_date)
                .on_conflict_do_update(
                    index_elements=["train_number", "travel_date"],
                    set_={"train_number": train_number},
                )
                .returning(Schedule.id)
            )
            session.execute(
                insert(Inventory)
                .values(
                    [
                        {
                            "schedule_id": schedule_id,
                            "class_code": code,
                            "total": seats,
                            "available": seats,
                            "fare": fare,
                        }
                        for code, (seats, fare) in CLASSES.items()
                    ]
                )
                .on_conflict_do_nothing()
            )
    session.commit()
    log.info("setup_complete", stations=len(STATIONS), trains=len(TRAINS), days=DAYS_AHEAD)


def main(argv: list[str]) -> int:
    configure_logging(get_settings().log_level)
    if argv != ["setup"]:
        print(__doc__)
        return 2
    with Session(get_engine()) as session:
        setup(session)
    get_engine().dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
