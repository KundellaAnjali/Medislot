import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("SLOTS_TABLE_NAME", "slots")
os.environ.setdefault("IDEMPOTENCY_TABLE_NAME", "idempotency")

import appointment_booking
from appointment_service import AppointmentService, AppointmentServiceError


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(
            2026,
            11,
            28,
            12,
            0,
            0,
            500000,
            tzinfo=timezone(timedelta(hours=5, minutes=30)),
        )


class AppointmentBookingTimeTests(unittest.TestCase):
    def test_booking_requests_require_ist_offset(self):
        body = {
            "doctor_id": 2,
            "slot_start_time": "2026-11-29T10:00:00+05:30",
        }
        request = {"body": json.dumps(body)}
        self.assertEqual(
            appointment_booking.parse_booking_request(request)[:2],
            (2, "2026-11-29T10:00:00+05:30"),
        )

        body["slot_start_time"] = "2026-11-29T10:00:00Z"
        with self.assertRaisesRegex(
            appointment_booking.BookingError,
            r"IST ISO-8601 timestamp with \+05:30 offset",
        ):
            appointment_booking.parse_booking_request(
                {"body": json.dumps(body)}
            )

    def test_booking_rejects_past_time_before_idempotency_lookup(self):
        idempotency_table = Mock()
        service = AppointmentService(
            repository=Mock(),
            idempotency_table=idempotency_table,
            get_slot=Mock(),
            validate_slot=Mock(),
            reserve_slot=Mock(),
            release_slot=Mock(),
        )

        with patch("appointment_service.datetime", FrozenDateTime):
            with self.assertRaisesRegex(
                AppointmentServiceError, "appointment slot is in the past"
            ):
                service.book(
                    user_id=1,
                    key="request-key",
                    doctor_id=2,
                    slot_start_time="2026-11-28T11:59:00+05:30",
                )

        idempotency_table.get_item.assert_not_called()

    def test_dynamodb_cutoff_rounds_up_to_avoid_string_precision_bypass(self):
        slot = {
            "slot_start_time": "2026-11-28T12:00:00.900000+05:30",
            "capacity": 2,
            "booked_count": 0,
            "version": 1,
            "status": 1,
        }

        with patch.object(appointment_booking, "datetime", FrozenDateTime):
            _, _, _, cutoff = appointment_booking.validate_slot(slot)

        self.assertEqual(
            cutoff.isoformat(),
            "2026-11-28T12:00:01+05:30",
        )
        self.assertLess(slot["slot_start_time"], cutoff.isoformat())

    def test_booking_validation_rejects_elapsed_slot(self):
        slot = {
            "slot_start_time": "2026-11-28T11:59:00+05:30",
            "capacity": 2,
            "booked_count": 0,
            "version": 1,
            "status": 1,
        }

        with patch.object(appointment_booking, "datetime", FrozenDateTime):
            with self.assertRaisesRegex(
                appointment_booking.BookingError,
                "appointment slot is in the past",
            ):
                appointment_booking.validate_slot(slot)


if __name__ == "__main__":
    unittest.main()
