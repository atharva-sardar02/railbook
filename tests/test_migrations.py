from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.admin import alembic_config, setup
from app.db import get_engine


def test_migrations_match_models():
    # Fails when a model changed without a migration: autogenerate would still find a difference.
    with get_engine().connect() as conn:
        command.check(alembic_config(conn))


def test_database_from_before_alembic_is_upgraded():
    # Rebuild the old situation: the 0001 tables exist, but nothing records a migration
    # (that's what create_all() left in the cluster), and there's already a booking.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        command.upgrade(alembic_config(conn), "0001")
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO stations VALUES ('AAA', 'A'), ('BBB', 'B')"))
        conn.execute(
            text(
                "INSERT INTO trains VALUES ('1', 'T', 'AAA', 'BBB', '10:00', '12:00');"
                "INSERT INTO schedules (train_number, travel_date) VALUES ('1', '2026-01-01');"
                "INSERT INTO bookings (pnr, schedule_id, class_code, passenger_name, seats, amount,"
                " status) SELECT 'OLD1', id, 'SL', 'Old', 1, 650, 'CONFIRMED' FROM schedules"
            )
        )

    with Session(engine) as session:
        setup(session)

    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        assert "user_id" in {c["name"] for c in inspect(conn).get_columns("bookings")}
        # The old booking survived, with no owner.
        assert conn.scalar(text("SELECT user_id FROM bookings WHERE pnr = 'OLD1'")) is None
