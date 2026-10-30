import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import payment
from app.auth import CurrentUser, User
from app.db import get_session
from app.metrics import BOOKING_DURATION, BOOKINGS, CANCELLATIONS
from app.models import Booking, Inventory, Schedule
from app.schemas import BookingOut, BookingRequest

router = APIRouter(tags=["bookings"])
log = structlog.get_logger("railbook.bookings")


def _new_pnr() -> str:
    return f"{secrets.randbelow(10**10):010d}"


def _to_out(booking: Booking, schedule: Schedule) -> BookingOut:
    return BookingOut(
        pnr=booking.pnr,
        train_number=schedule.train_number,
        travel_date=schedule.travel_date,
        class_code=booking.class_code,
        passenger_name=booking.passenger_name,
        seats=booking.seats,
        amount=booking.amount,
        status=booking.status,
    )


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def create_booking(
    req: BookingRequest, user: CurrentUser, session: Annotated[Session, Depends(get_session)]
) -> BookingOut:
    with BOOKING_DURATION.time(), session.begin():
        schedule = session.scalar(
            select(Schedule).where(
                Schedule.train_number == req.train_number,
                Schedule.travel_date == req.travel_date,
            )
        )
        inv = (
            session.scalar(
                select(Inventory)
                .where(
                    Inventory.schedule_id == schedule.id,
                    Inventory.class_code == req.class_code.upper(),
                )
                .with_for_update()  # serialises concurrent bookings for this class
            )
            if schedule
            else None
        )
        if inv is None:
            BOOKINGS.labels(status="not_found").inc()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "train, date or class not found")

        if inv.available < req.seats:
            BOOKINGS.labels(status="sold_out").inc()
            log.info("booking_rejected", reason="sold_out", available=inv.available)
            raise HTTPException(status.HTTP_409_CONFLICT, f"only {inv.available} seats left")

        amount = inv.fare * req.seats
        try:
            payment.charge(amount, simulate_failure=req.simulate_payment_failure)
        except payment.PaymentError as exc:
            BOOKINGS.labels(status="payment_failed").inc()
            log.warning("booking_rejected", reason="payment_failed", error=str(exc))
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc

        inv.available -= req.seats
        booking = Booking(
            pnr=_new_pnr(),
            schedule_id=schedule.id,
            class_code=inv.class_code,
            passenger_name=req.passenger_name,
            seats=req.seats,
            amount=amount,
            status="CONFIRMED",
            user_id=user.sub,  # the owner is whoever the token says, never a request field
        )
        session.add(booking)

    BOOKINGS.labels(status="confirmed").inc()
    structlog.contextvars.bind_contextvars(pnr=booking.pnr)
    log.info("booking_confirmed", seats=booking.seats, class_code=booking.class_code)
    return _to_out(booking, schedule)


def _load(
    session: Session, pnr: str, user: User, lock: bool = False
) -> tuple[Booking, Schedule]:
    query = select(Booking, Schedule).join(Schedule).where(Booking.pnr == pnr)
    if lock:
        query = query.with_for_update(of=Booking)
    row = session.execute(query).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "PNR not found")
    structlog.contextvars.bind_contextvars(pnr=pnr)
    # Authorization: the owner or an admin. Bookings from before login have no owner
    # (user_id NULL), so only admins pass.
    if row.Booking.user_id != user.sub and not user.is_admin:
        log.info("booking_forbidden", owner_matches=False)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your booking")
    return row.Booking, row.Schedule


@router.get("/bookings", response_model=list[BookingOut])
def list_bookings(
    user: CurrentUser, session: Annotated[Session, Depends(get_session)]
) -> list[BookingOut]:
    """Your bookings, newest first (an admin sees everyone's)."""
    query = select(Booking, Schedule).join(Schedule).order_by(Booking.created_at.desc()).limit(50)
    if not user.is_admin:
        query = query.where(Booking.user_id == user.sub)
    return [_to_out(row.Booking, row.Schedule) for row in session.execute(query)]


@router.get("/bookings/{pnr}", response_model=BookingOut)
def get_booking(
    pnr: str, user: CurrentUser, session: Annotated[Session, Depends(get_session)]
) -> BookingOut:
    return _to_out(*_load(session, pnr, user))


@router.delete("/bookings/{pnr}", response_model=BookingOut)
def cancel_booking(
    pnr: str, user: CurrentUser, session: Annotated[Session, Depends(get_session)]
) -> BookingOut:
    with session.begin():
        booking, schedule = _load(session, pnr, user, lock=True)
        if booking.status == "CANCELLED":
            raise HTTPException(status.HTTP_409_CONFLICT, "booking already cancelled")
        inv = session.scalar(
            select(Inventory)
            .where(
                Inventory.schedule_id == booking.schedule_id,
                Inventory.class_code == booking.class_code,
            )
            .with_for_update()
        )
        inv.available += booking.seats
        booking.status = "CANCELLED"

    CANCELLATIONS.inc()
    log.info("booking_cancelled", seats=booking.seats)
    return _to_out(booking, schedule)
