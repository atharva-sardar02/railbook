from datetime import date, time
from decimal import Decimal

from pydantic import BaseModel, Field


class ClassAvailability(BaseModel):
    class_code: str
    available: int
    fare: Decimal


class TrainResult(BaseModel):
    number: str
    name: str
    from_station: str
    to_station: str
    departs_at: time
    arrives_at: time
    travel_date: date
    classes: list[ClassAvailability]


class BookingRequest(BaseModel):
    train_number: str
    travel_date: date
    class_code: str
    passenger_name: str = Field(min_length=1, max_length=100)
    seats: int = Field(ge=1, le=6)
    simulate_payment_failure: bool = False


class BookingOut(BaseModel):
    pnr: str
    train_number: str
    travel_date: date
    class_code: str
    passenger_name: str
    seats: int
    amount: Decimal
    status: str
