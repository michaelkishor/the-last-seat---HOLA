import random
import uuid
from locust import HttpUser, task, between

class ConcertBookingUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(3)
    def view_seats(self):
        self.client.get("/seats?status=available")

    @task(5)
    def attempt_booking_flow(self):
        user_id = f"student_{uuid.uuid4().hex[:8]}"
        seat_id = random.randint(1, 500)
        idem_key = str(uuid.uuid4())

        res = self.client.post(
            "/reserve",
            json={"user_id": user_id, "seat_id": seat_id},
            headers={"Idempotency-Key": idem_key}
        )

        if res.status_code == 200:
            data = res.json()
            booking_id = data["booking_id"]
            mode = random.choices(["success", "fail", "delayed"], weights=[75, 15, 10])[0]
            self.client.post(
                "/confirm",
                json={
                    "booking_id": booking_id,
                    "user_id": user_id,
                    "payment_mode": mode,
                    "delay_seconds": 1
                },
                headers={"Idempotency-Key": str(uuid.uuid4())}
            )
        elif res.status_code == 409:
            if "waitlist" in res.text:
                self.client.post("/waitlist", json={"user_id": user_id})