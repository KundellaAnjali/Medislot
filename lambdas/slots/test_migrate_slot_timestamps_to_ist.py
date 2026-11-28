import unittest

from migrate_slot_timestamps_to_ist import build_plans, india_timestamp


class FakeTable:
    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": self.items}


class SlotTimestampMigrationTests(unittest.TestCase):
    def test_preserves_clock_fields_when_rewriting_legacy_utc_timestamps(self):
        self.assertEqual(
            india_timestamp("2026-12-01T10:00:00Z"),
            "2026-12-01T10:00:00+05:30",
        )

    def test_plans_slot_and_related_record_updates(self):
        tables = {
            "slots": FakeTable(
                [
                    {
                        "doctor_id": 11,
                        "slot_start_time": "2026-12-01T10:00:00Z",
                        "slot_end_time": "2026-12-01T10:30:00Z",
                        "version": 1,
                    }
                ]
            ),
            "waitlists": FakeTable(
                [
                    {
                        "slot_id": 22,
                        "joined_at": "WAITLIST#1",
                        "slot_start_time": "2026-12-01T10:00:00Z",
                    }
                ]
            ),
            "offers": FakeTable(
                [
                    {
                        "offer_id": 33,
                        "slot_start_time": "2026-12-01T10:00:00Z",
                    }
                ]
            ),
            "idempotency": FakeTable(
                [
                    {
                        "idempotency_key": (
                            "waitlist-active-offer:11:2026-12-01T10:00:00Z"
                        ),
                        "offer_id": 33,
                    },
                    {
                        "idempotency_key": "booking-key",
                        "response_payload": (
                            '{"slot_start_time":"2026-12-01T10:00:00Z"}'
                        ),
                    },
                ]
            ),
        }

        plans = build_plans(tables)
        converted = {
            (table_name, old_item.get("idempotency_key")): new_item
            for table_name, old_item, new_item in plans
        }

        self.assertEqual(len(plans), 5)
        self.assertEqual(
            converted[("slots", None)]["slot_end_time"],
            "2026-12-01T10:30:00+05:30",
        )
        self.assertEqual(
            converted[
                (
                    "idempotency",
                    "waitlist-active-offer:11:2026-12-01T10:00:00Z",
                )
            ]["idempotency_key"],
            "waitlist-active-offer:11:2026-12-01T10:00:00+05:30",
        )
        self.assertEqual(
            converted[("idempotency", "booking-key")]["response_payload"],
            '{"slot_start_time":"2026-12-01T10:00:00+05:30"}',
        )

    def test_rejects_slot_key_collisions(self):
        tables = {
            "slots": FakeTable(
                [
                    {
                        "doctor_id": 11,
                        "slot_start_time": "2026-12-01T10:00:00Z",
                    },
                    {
                        "doctor_id": 11,
                        "slot_start_time": "2026-12-01T10:00:00+05:30",
                    },
                ]
            ),
            "waitlists": FakeTable([]),
            "offers": FakeTable([]),
            "idempotency": FakeTable([]),
        }

        with self.assertRaisesRegex(RuntimeError, "Slot key collision"):
            build_plans(tables)


if __name__ == "__main__":
    unittest.main()
