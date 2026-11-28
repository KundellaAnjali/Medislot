import os
import unittest
from unittest.mock import patch

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("SLOTS_TABLE_NAME", "slots")
os.environ.setdefault("IDEMPOTENCY_TABLE_NAME", "idempotency")
os.environ.setdefault("WAITLIST_TABLE_NAME", "waitlists")
os.environ.setdefault("WAITLIST_OFFERS_TABLE_NAME", "offers")

import reschedule_workflow
import waitlist_workflow

from boto3.dynamodb.types import TypeDeserializer


class RescheduleCursor:
    def __init__(self):
        self.appointment = {
            "slot_id": 22,
            "slot_start_time": "2026-11-02T11:00:00+05:30",
            "status": "BOOKED",
            "appointment_status": "CONFIRMED",
        }
        self.appointments = [self.appointment]
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, parameters=None):
        if query.lstrip().startswith("UPDATE appointments"):
            self.appointment.update(
                slot_id=parameters[0],
                slot_start_time=parameters[1].isoformat(),
            )
            self.rowcount = 1

    def fetchone(self):
        return self.appointment.copy()


class RescheduleConnection:
    def __init__(self):
        self.cursor_instance = RescheduleCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class FakeTable:
    def __init__(self, name, state):
        self.name = name
        self.state = state

    def get_item(self, **kwargs):
        if self.name == "offers":
            offer_id = kwargs["Key"]["offer_id"]
            offer = self.state["offers"].get(offer_id)
            return {"Item": offer} if offer else {}
        if self.name == "slots":
            return {"Item": self.state["slot"]}
        return {}

    def query(self, **kwargs):
        return {"Items": self.state["queue"]}

    def update_item(self, **kwargs):
        offer_id = kwargs["Key"]["offer_id"]
        self.state["offers"][offer_id]["event_published"] = True


class FakeDynamoDB:
    def __init__(self, state):
        self.state = state

    def Table(self, name):
        return FakeTable(name, self.state)


class FakeDynamoClient:
    def __init__(self, state):
        self.state = state
        self.transaction_count = 0
        self.deserializer = TypeDeserializer()

    def get_item(self, **kwargs):
        if self.state["lock"]:
            return {
                "Item": {
                    "offer_id": {
                        "N": str(self.state["lock"]["offer_id"])
                    }
                }
            }
        return {}

    def transact_write_items(self, *, TransactItems):
        self.transaction_count += 1
        lock_item = TransactItems[0]["Put"]["Item"]
        lock = {
            name: self.deserializer.deserialize(value)
            for name, value in lock_item.items()
        }
        self.state["lock"] = lock
        slot_update = TransactItems[1]["Update"]
        slot_values = {
            name: self.deserializer.deserialize(value)
            for name, value in slot_update["ExpressionAttributeValues"].items()
        }
        self.state["slot"]["booked_count"] += slot_values[":one"]
        self.state["slot"]["version"] += slot_values[":one"]
        offer_item = TransactItems[2]["Put"]["Item"]
        offer = {
            name: self.deserializer.deserialize(value)
            for name, value in offer_item.items()
        }
        self.state["offers"][offer["offer_id"]] = offer


class Milestone6IntegrationTests(unittest.TestCase):
    def test_reschedule_compensation_keeps_one_appointment_after_release_failure(self):
        connection = RescheduleConnection()
        claim = {
            "appointment_id": 7,
            "patient_id": 3,
            "doctor_id": 5,
            "attempt": 1,
            "original_slot_id": 11,
            "original_slot_start_time": "2026-11-02T10:00:00+05:30",
            "target_slot_id": 22,
            "target_slot_start_time": "2026-11-02T11:00:00+05:30",
        }

        with patch.object(
            reschedule_workflow,
            "release_slot",
            side_effect=RuntimeError("forced original-slot release failure"),
        ):
            with self.assertRaises(RuntimeError):
                reschedule_workflow.release_original({"claim": claim}, None)

        with (
            patch.object(reschedule_workflow, "get_connection", return_value=connection),
            patch.object(reschedule_workflow, "release_slot") as release_slot,
            patch.object(reschedule_workflow, "publish_metric"),
        ):
            result = reschedule_workflow.compensate({"claim": claim}, None)

        self.assertTrue(result["compensated"])
        self.assertEqual(connection.cursor_instance.appointment["slot_id"], 11)
        self.assertEqual(
            connection.cursor_instance.appointment["slot_start_time"],
            "2026-11-02T10:00:00",
        )
        self.assertEqual(len(connection.cursor_instance.appointments), 1)
        release_slot.assert_called_once_with(
            claim,
            slot_id=22,
            start_time="2026-11-02T11:00:00+05:30",
            marker_kind="compensation",
        )

    def test_multiple_waitlisted_patients_create_only_one_active_offer(self):
        state = {
            "slot": {
                "slot_id": 9,
                "status": 1,
                "booked_count": 0,
                "capacity": 1,
                "version": 1,
            },
            "lock": None,
            "offers": {},
            "queue": [
                {
                    "patient_id": 1,
                    "consultation_type_id": 2,
                    "waitlist_id": "first",
                    "joined_at": "2026-11-01T10:00:00+05:30",
                    "status": "WAITING",
                },
                {
                    "patient_id": 2,
                    "consultation_type_id": 2,
                    "waitlist_id": "second",
                    "joined_at": "2026-11-01T10:01:00+05:30",
                    "status": "WAITING",
                },
                {
                    "patient_id": 3,
                    "consultation_type_id": 2,
                    "waitlist_id": "third",
                    "joined_at": "2026-11-01T10:02:00+05:30",
                    "status": "WAITING",
                },
            ],
        }
        fake_dynamodb = FakeDynamoDB(state)
        fake_client = FakeDynamoClient(state)
        event = {
            "doctor_id": 5,
            "slot_id": 9,
            "slot_start_time": "2026-11-02T10:00:00+05:30",
            "status": "SEAT_FREED",
        }

        with (
            patch.object(waitlist_workflow, "dynamodb", fake_dynamodb),
            patch.object(waitlist_workflow, "waitlists", FakeTable("waitlists", state)),
            patch.object(waitlist_workflow, "offers", FakeTable("offers", state)),
            patch.object(waitlist_workflow, "slots", FakeTable("slots", state)),
            patch.object(waitlist_workflow, "client", fake_client),
            patch.object(waitlist_workflow, "get_parameter", return_value=15),
            patch.object(waitlist_workflow, "publish_event"),
            patch.object(waitlist_workflow, "publish_metric"),
        ):
            first_result = waitlist_workflow.promote_slot(event)
            second_result = waitlist_workflow.promote_slot(event)

        self.assertEqual(first_result["offer_id"], second_result["offer_id"])
        self.assertEqual(fake_client.transaction_count, 1)
        self.assertEqual(len(state["offers"]), 1)
        self.assertEqual(state["offers"][first_result["offer_id"]]["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
