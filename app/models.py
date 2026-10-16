from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from datetime import datetime
from .database import Base

class Seat(Base):
    __tablename__ = "seats"
    id = Column(Integer, primary_key=True, index=True)
    seat_number = Column(String, unique=True, index=True)
    status = Column(String, default="available")
    reserved_by = Column(String, nullable=True)
    reserved_until = Column(DateTime, nullable=True)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    seat_id = Column(Integer, ForeignKey("seats.id"))
    status = Column(String, default="pending")
    payment_status = Column(String, default="unpaid")
    idempotency_key = Column(String, unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)

class Waitlist(Base):
    __tablename__ = "waitlist"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True)
    status = Column(String, default="waiting")
    allocated_seat_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)