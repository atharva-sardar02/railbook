from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Inventory, Schedule, Train
from app.schemas import ClassAvailability, TrainResult

router = APIRouter(tags=["trains"])


@router.get("/trains", response_model=list[TrainResult])
def search_trains(
    session: Annotated[Session, Depends(get_session)],
    from_station: Annotated[str, Query(alias="from")],
    to_station: Annotated[str, Query(alias="to")],
    travel_date: Annotated[date, Query(alias="date")],
) -> list[TrainResult]:
    rows = session.execute(
        select(Train, Schedule, Inventory)
        .join(Schedule, Schedule.train_number == Train.number)
        .join(Inventory, Inventory.schedule_id == Schedule.id)
        .where(
            Train.from_station == from_station.upper(),
            Train.to_station == to_station.upper(),
            Schedule.travel_date == travel_date,
        )
        .order_by(Train.departs_at, Inventory.class_code)
    ).all()

    results: dict[str, TrainResult] = {}
    for train, schedule, inv in rows:
        result = results.setdefault(
            train.number,
            TrainResult(
                number=train.number,
                name=train.name,
                from_station=train.from_station,
                to_station=train.to_station,
                departs_at=train.departs_at,
                arrives_at=train.arrives_at,
                travel_date=schedule.travel_date,
                classes=[],
            ),
        )
        result.classes.append(
            ClassAvailability(class_code=inv.class_code, available=inv.available, fare=inv.fare)
        )
    return list(results.values())
