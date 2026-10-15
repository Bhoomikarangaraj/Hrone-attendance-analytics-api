from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import (
    attendance_date_for_punch,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    app,
)


def test_health_endpoint_returns_ok():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_late_minutes_follows_grace_and_flooring():
    punch_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(punch_in, "09:30", "18:30") == 0

    punch_in_late = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(punch_in_late, "09:30", "18:30") == 45


def test_overtime_after_shift_end_only_if_30_minutes_or_more():
    punch_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    punch_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(punch_out, "09:30", "18:30", punch_in) == 0

    punch_out = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(punch_out, "09:30", "18:30", punch_in) == 30


def test_work_hours_round_half_up():
    start = datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 6, 12, 15, 1, tzinfo=timezone.utc)
    assert compute_work_hours(start, end) == 3.25


def test_overnight_shift_uses_previous_day_for_attendance_date():
    punch_in = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert attendance_date_for_punch(punch_in, "22:00", "06:00") == "2026-07-06"

    punch_in = datetime(2026, 7, 6, 18, 40, 0, tzinfo=timezone.utc)
    assert attendance_date_for_punch(punch_in, "22:00", "06:00") == "2026-07-06"
