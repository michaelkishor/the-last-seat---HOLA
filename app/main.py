import json
import time
import random
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session
from .database import engine, SessionLocal, get_db, redis_client
from .models import Base, Seat, Booking, Waitlist

Base.metadata.create_all(bind=engine)

app = FastAPI(title="The Last Seat API")

RESERVATION_TIMEOUT_SECONDS = 120

class ReserveRequest(BaseModel):
    user_id: str
    seat_id: int

class ConfirmRequest(BaseModel):
    booking_id: int
    user_id: str
    payment_mode: str = "success"
    delay_seconds: int = 2

class WaitlistRequest(BaseModel):
    user_id: str

@app.on_event("startup")
def seed_seats():
    db = SessionLocal()
    try:
        count = db.query(Seat).count()
        if count == 0:
            seats = [Seat(seat_number=f"S-{i}", status="available") for i in range(1, 501)]
            db.add_all(seats)
            db.commit()
    finally:
        db.close()

def cleanup_expired_reservations(db: Session):
    now = datetime.utcnow()
    expired_bookings = db.query(Booking).filter(
        Booking.status == "pending",
        Booking.expires_at < now
    ).with_for_update(skip_locked=True).all()

    for b in expired_bookings:
        b.status = "expired"
        b.payment_status = "timeout"
        seat = db.query(Seat).filter(Seat.id == b.seat_id).with_for_update().first()
        if seat and seat.status == "reserved":
            reallocate_or_free_seat(db, seat)
    if expired_bookings:
        db.commit()

def reallocate_or_free_seat(db: Session, seat: Seat):
    next_in_line = db.query(Waitlist).filter(
        Waitlist.status == "waiting"
    ).order_by(Waitlist.created_at.asc()).with_for_update(skip_locked=True).first()

    if next_in_line:
        now = datetime.utcnow()
        exp = now + timedelta(seconds=RESERVATION_TIMEOUT_SECONDS)
        seat.status = "reserved"
        seat.reserved_by = next_in_line.user_id
        seat.reserved_until = exp

        new_booking = Booking(
            user_id=next_in_line.user_id,
            seat_id=seat.id,
            status="pending",
            payment_status="unpaid",
            created_at=now,
            expires_at=exp
        )
        db.add(new_booking)
        next_in_line.status = "allocated"
        next_in_line.allocated_seat_id = seat.id
    else:
        seat.status = "available"
        seat.reserved_by = None
        seat.reserved_until = None

@app.get("/seats")
def get_seats(status: Optional[str] = None, db: Session = Depends(get_db)):
    cleanup_expired_reservations(db)
    query = db.query(Seat)
    if status:
        query = query.filter(Seat.status == status)
    seats = query.order_by(Seat.id.asc()).all()
    return {
        "total_count": len(seats),
        "seats": [
            {
                "id": s.id,
                "seat_number": s.seat_number,
                "status": s.status,
                "reserved_until": s.reserved_until.isoformat() if s.reserved_until else None
            }
            for s in seats
        ]
    }

@app.post("/reserve")
def reserve_seat(
    req: ReserveRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    if idempotency_key:
        cached = redis_client.get(f"idem:reserve:{idempotency_key}")
        if cached:
            return json.loads(cached)

    cleanup_expired_reservations(db)

    seat = db.query(Seat).filter(Seat.id == req.seat_id).with_for_update().first()
    if not seat:
        raise HTTPException(status_code=404, detail="Seat not found")

    if seat.status != "available":
        available_count = db.query(Seat).filter(Seat.status == "available").count()
        if available_count == 0:
            raise HTTPException(
                status_code=409,
                detail="All seats are currently taken or reserved. Please join the /waitlist."
            )
        raise HTTPException(status_code=409, detail=f"Seat is already {seat.status}")

    now = datetime.utcnow()
    expires = now + timedelta(seconds=RESERVATION_TIMEOUT_SECONDS)

    seat.status = "reserved"
    seat.reserved_by = req.user_id
    seat.reserved_until = expires

    booking = Booking(
        user_id=req.user_id,
        seat_id=seat.id,
        status="pending",
        payment_status="unpaid",
        idempotency_key=idempotency_key,
        created_at=now,
        expires_at=expires
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    response_data = {
        "message": "Seat reserved temporarily",
        "booking_id": booking.id,
        "seat_id": seat.id,
        "seat_number": seat.seat_number,
        "status": booking.status,
        "expires_at": booking.expires_at.isoformat()
    }

    if idempotency_key:
        redis_client.setex(f"idem:reserve:{idempotency_key}", 300, json.dumps(response_data))

    return response_data

@app.post("/confirm")
def confirm_booking(
    req: ConfirmRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db)
):
    if idempotency_key:
        cached = redis_client.get(f"idem:confirm:{idempotency_key}")
        if cached:
            return json.loads(cached)

    if req.payment_mode == "delayed":
        time.sleep(req.delay_seconds)

    cleanup_expired_reservations(db)

    booking = db.query(Booking).filter(Booking.id == req.booking_id).with_for_update().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.user_id != req.user_id:
        raise HTTPException(status_code=403, detail="Unauthorized user for this booking")

    if booking.status == "confirmed":
        return {"message": "Booking already confirmed", "booking_id": booking.id, "status": "confirmed"}

    if booking.status == "expired" or (booking.expires_at and booking.expires_at < datetime.utcnow()):
        booking.status = "expired"
        booking.payment_status = "timeout"
        seat = db.query(Seat).filter(Seat.id == booking.seat_id).with_for_update().first()
        if seat and seat.status == "reserved" and seat.reserved_by == req.user_id:
            reallocate_or_free_seat(db, seat)
        db.commit()
        raise HTTPException(status_code=400, detail="Reservation expired before payment completed")

    if booking.status != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot confirm booking with status {booking.status}")

    seat = db.query(Seat).filter(Seat.id == booking.seat_id).with_for_update().first()

    outcome = req.payment_mode
    if outcome == "random":
        outcome = random.choice(["success", "fail"])

    if outcome == "fail":
        booking.status = "failed"
        booking.payment_status = "failed"
        if seat:
            reallocate_or_free_seat(db, seat)
        db.commit()
        res = {
            "message": "Payment failed, seat has been released",
            "booking_id": booking.id,
            "status": booking.status,
            "payment_status": booking.payment_status
        }
        if idempotency_key:
            redis_client.setex(f"idem:confirm:{idempotency_key}", 300, json.dumps(res))
        return res

    booking.status = "confirmed"
    booking.payment_status = "paid"
    seat.status = "booked"
    seat.reserved_until = None
    db.commit()

    res = {
        "message": "Booking confirmed successfully",
        "booking_id": booking.id,
        "seat_id": seat.id,
        "seat_number": seat.seat_number,
        "status": booking.status,
        "payment_status": booking.payment_status
    }
    if idempotency_key:
        redis_client.setex(f"idem:confirm:{idempotency_key}", 300, json.dumps(res))
    return res

@app.post("/cancel/{booking_id}")
def cancel_booking(booking_id: int, user_id: str, db: Session = Depends(get_db)):
    cleanup_expired_reservations(db)
    booking = db.query(Booking).filter(Booking.id == booking_id).with_for_update().first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    if booking.user_id != user_id:
        raise HTTPException(status_code=403, detail="Not your booking")

    if booking.status not in ["pending", "confirmed"]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel booking in {booking.status} state")

    booking.status = "cancelled"
    if booking.payment_status == "paid":
        booking.payment_status = "refunded"

    seat = db.query(Seat).filter(Seat.id == booking.seat_id).with_for_update().first()
    if seat:
        reallocate_or_free_seat(db, seat)

    db.commit()
    return {
        "message": "Booking cancelled and seat reallocated/released",
        "booking_id": booking.id,
        "status": booking.status,
        "payment_status": booking.payment_status
    }

@app.post("/waitlist")
def join_waitlist(req: WaitlistRequest, db: Session = Depends(get_db)):
    existing = db.query(Waitlist).filter(
        Waitlist.user_id == req.user_id,
        Waitlist.status == "waiting"
    ).first()
    if existing:
        return {"message": "Already on waitlist", "waitlist_id": existing.id}

    entry = Waitlist(user_id=req.user_id, status="waiting")
    db.add(entry)
    db.commit()
    db.refresh(entry)
    position = db.query(Waitlist).filter(
        Waitlist.status == "waiting",
        Waitlist.id <= entry.id
    ).count()
    return {
        "message": "Added to waiting queue",
        "waitlist_id": entry.id,
        "position": position
    }

@app.get("/bookings/{booking_id}")
def get_booking_status(booking_id: int, db: Session = Depends(get_db)):
    cleanup_expired_reservations(db)
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {
        "booking_id": booking.id,
        "user_id": booking.user_id,
        "seat_id": booking.seat_id,
        "status": booking.status,
        "payment_status": booking.payment_status,
        "created_at": booking.created_at.isoformat(),
        "expires_at": booking.expires_at.isoformat() if booking.expires_at else None
    }

@app.get("/bookings/user/{user_id}")
def get_user_booking_history(user_id: str, db: Session = Depends(get_db)):
    cleanup_expired_reservations(db)
    bookings = db.query(Booking).filter(Booking.user_id == user_id).order_by(Booking.created_at.desc()).all()
    waitlist_entries = db.query(Waitlist).filter(Waitlist.user_id == user_id).all()
    return {
        "user_id": user_id,
        "bookings": [
            {
                "booking_id": b.id,
                "seat_id": b.seat_id,
                "status": b.status,
                "payment_status": b.payment_status,
                "created_at": b.created_at.isoformat(),
                "expires_at": b.expires_at.isoformat() if b.expires_at else None
            }
            for b in bookings
        ],
        "waitlist": [
            {
                "waitlist_id": w.id,
                "status": w.status,
                "allocated_seat_id": w.allocated_seat_id
            }
            for w in waitlist_entries
        ]
    }