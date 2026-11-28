import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("WAITLIST_TABLE_NAME", "waitlists")
os.environ.setdefault("SLOTS_TABLE_NAME", "slots")
os.environ.setdefault("IDEMPOTENCY_TABLE_NAME", "idempotency")

import waitlist_api


class WaitlistBookingExclusivityTests(unittest.TestCase):
    def test_patient_with_active_booking_cannot_join_same_full_slot(self):
        cursor = Mock()
        cursor.fetchone.return_value = {"1": 1}
        connection = Mock()
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection.cursor.return_value = cursor_context

        with (
            patch.object(waitlist_api, "get_connection", return_value=connection),
            patch.object(waitlist_api, "india_now") as now,
            patch.object(waitlist_api.slots, "get_item") as get_slot,
            patch.object(waitlist_api, "get_parameter", return_value=5),
            patch.object(waitlist_api.client, "transact_write_items") as transact,
        ):
            now.return_value = datetime(
                2026,
                11,
                28,
                12,
                0,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            )
            get_slot.return_value = {
                "Item": {
                    "slot_id": 88,
                    "status": 1,
                    "booked_count": 1,
                    "capacity": 1,
                    "consultation_type_id": 4,
                }
            }

            with self.assertRaisesRegex(
                waitlist_api.WaitlistError,
                "already have an active appointment",
            ):
                waitlist_api.join_waitlist(
                    17,
                    {
                        "doctor_id": 5,
                        "slot_id": 88,
                        "slot_start_time": "2026-11-29T10:00:00+05:30",
                    },
                )

        cursor.execute.assert_called_once()
        transact.assert_not_called()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
