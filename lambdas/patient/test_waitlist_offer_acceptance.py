import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("WAITLIST_OFFERS_TABLE_NAME", "offers")
os.environ.setdefault("WAITLIST_TABLE_NAME", "waitlists")
os.environ.setdefault("SLOTS_TABLE_NAME", "slots")
os.environ.setdefault("IDEMPOTENCY_TABLE_NAME", "idempotency")

import waitlist_offer_api
from milestone6_common import active_offer_lock_key


class WaitlistOfferAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.offer = {
            "offer_id": 123,
            "patient_id": 7,
            "doctor_id": 11,
            "slot_id": 22,
            "slot_start_time": "2026-12-01T10:00:00+05:30",
            "consultation_type_id": 4,
            "payment_method": "UPI",
            "waitlist_id": "waitlist-abc",
            "joined_at": "2026-11-28T10:00:00Z#waitlist-abc",
            "status": "ACTIVE",
            "expires_at": 1796000000,
        }

    def test_accept_offer_creates_appointment_and_commits_offer(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"patient_id": 7},
            {"active_count": 0},
            {"fee": 125},
        ]
        cursor.lastrowid = 456
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        connection = MagicMock()
        connection.cursor.return_value = cursor_context
        now = datetime(
            2026, 11, 28, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )

        with (
            patch.object(
                waitlist_offer_api.offers,
                "get_item",
                return_value={"Item": self.offer},
            ),
            patch.object(waitlist_offer_api, "get_connection", return_value=connection),
            patch.object(waitlist_offer_api, "get_parameter", return_value=3),
            patch.object(waitlist_offer_api, "commit_offer") as commit_offer,
            patch.object(waitlist_offer_api, "publish_event"),
            patch.object(waitlist_offer_api, "publish_metric"),
        ):
            result = waitlist_offer_api.accept_offer(
                7,
                123,
                now=now,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["appointment_id"], 456)
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertTrue(
            any(
                call.args[0].lstrip().startswith("INSERT INTO appointments")
                for call in cursor.execute.call_args_list
            )
        )
        self.assertTrue(
            any(
                call.args[0].lstrip().startswith("INSERT INTO payments")
                for call in cursor.execute.call_args_list
            )
        )
        commit_offer.assert_called_once()
        connection.commit.assert_called_once()
        connection.close.assert_called_once()

    def test_commit_offer_deletes_the_correct_active_offer_lock(self):
        now = datetime(
            2026, 11, 28, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
        with (
            patch.object(waitlist_offer_api.client, "transact_write_items") as transact,
            patch.object(waitlist_offer_api, "utc_now", return_value=now),
        ):
            waitlist_offer_api.commit_offer(self.offer, 456, "PAY-TEST")

        transaction = transact.call_args.kwargs["TransactItems"]
        lock_delete = transaction[1]["Delete"]
        self.assertEqual(
            lock_delete["Key"]["idempotency_key"]["S"],
            active_offer_lock_key(11, "2026-12-01T10:00:00+05:30"),
        )


if __name__ == "__main__":
    unittest.main()
