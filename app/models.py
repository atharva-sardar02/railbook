from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Station(Base):
    __tablename__ = "stations"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Train(Base):
    __tablename__ = "trains"

    number: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    from_station: Mapped[str] = mapped_column(ForeignKey("stations.code"))
    to_station: Mapped[str] = mapped_column(ForeignKey("stations.code"))
    departs_at: Mapped[time] = mapped_column(Time)
    arrives_at: Mapped[time] = mapped_column(Time)


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("train_number", "travel_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    train_number: Mapped[str] = mapped_column(ForeignKey("trains.number"))
    travel_date: Mapped[date] = mapped_column(Date, index=True)


class Inventory(Base):
    """Seats per class for one schedule. Rows are locked with SELECT ... FOR UPDATE."""

    __tablename__ = "inventory"
    __table_args__ = (CheckConstraint("available >= 0 AND available <= total"),)

    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"), primary_key=True)
    class_code: Mapped[str] = mapped_column(String(4), primary_key=True)
    total: Mapped[int] = mapped_column(Integer)
    available: Mapped[int] = mapped_column(Integer)
    fare: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class Booking(Base):
    __tablename__ = "bookings"

    pnr: Mapped[str] = mapped_column(String(10), primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"))
    class_code: Mapped[str] = mapped_column(String(4))
    passenger_name: Mapped[str] = mapped_column(String(100))
    seats: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
