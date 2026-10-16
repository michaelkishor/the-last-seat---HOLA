import uuid
import requests
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "http://localhost:8080"
TARGET_SEAT = 25

def try_reserve(user_num):
    user_id = f"student_{user_num}"
    idem_key = str(uuid.uuid4())
    resp = requests.post(
        f"{BASE_URL}/reserve",
        json={"user_id": user_id, "seat_id": TARGET_SEAT},
        headers={"Idempotency-Key": idem_key}
    )
    return user_id, resp.status_code, resp.json()

def run_test():
    print(f"Sending 50 simultaneous requests for Seat {TARGET_SEAT}...")
    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(try_reserve, range(1, 51)))

    successes = [r for r in results if r[1] == 200]
    conflicts = [r for r in results if r[1] == 409]

    print(f"Total Requests Sent: {len(results)}")
    print(f"Successful Reservations (200 OK): {len(successes)}")
    print(f"Blocked Duplicate Attempts (409 Conflict): {len(conflicts)}")

    if len(successes) == 1:
        winner_user, _, data = successes[0]
        booking_id = data["booking_id"]
        print(f"Winner: {winner_user} with Booking ID {booking_id}")

        confirm = requests.post(
            f"{BASE_URL}/confirm",
            json={
                "booking_id": booking_id,
                "user_id": winner_user,
                "payment_mode": "success"
            },
            headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        print("Payment Confirmation Response:", confirm.json())

if __name__ == "__main__":
    run_test()