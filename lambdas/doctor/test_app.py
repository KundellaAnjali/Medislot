import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("SLOTS_TABLE_NAME", "test-slots")
os.environ.setdefault("PHOTO_BUCKET_NAME", "test-photos")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")

from botocore.exceptions import ClientError

import app


class SlotMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.doctor = {"doctor_id": 14}
        self.pattern = {
            "consultation_type_id": 3,
            "consultation_name": "Detailed Consult",
            "day_of_week": "SUNDAY",
            "start_time": timedelta(hours=9),
            "end_time": timedelta(hours=11),
            "duration_minutes": 60,
            "capacity": 1,
        }

    def test_materializes_availability_in_utc(self):
        with patch.object(app.slots_table, "put_item") as put_item:
            created = app.materialize_for_doctor_day(
                self.doctor, [self.pattern], [], date(2027, 3, 14)
            )

        self.assertEqual(created, 2)
        starts = [call.kwargs["Item"]["start_time"] for call in put_item.call_args_list]
        self.assertEqual(
            starts,
            ["2027-03-14T09:00:00Z", "2027-03-14T10:00:00Z"],
        )

    def test_rerunning_materialization_does_not_create_duplicate_slots(self):
        slot_ids = set()

        def put_once(Item, ConditionExpression):
            if Item["slot_id"] in slot_ids:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "PutItem",
                )
            slot_ids.add(Item["slot_id"])

        with patch.object(app.slots_table, "put_item", side_effect=put_once):
            first_run = app.materialize_for_doctor_day(
                self.doctor, [self.pattern], [], date(2027, 3, 14)
            )
            second_run = app.materialize_for_doctor_day(
                self.doctor, [self.pattern], [], date(2027, 3, 14)
            )

        self.assertEqual(first_run, 2)
        self.assertEqual(second_run, 0)
        self.assertEqual(len(slot_ids), 2)

    def test_blackout_suppresses_materialized_slots(self):
        blackout = {
            "start_datetime": datetime(2027, 3, 14),
            "end_datetime": datetime(2027, 3, 15),
        }
        with patch.object(app.slots_table, "put_item") as put_item:
            created = app.materialize_for_doctor_day(
                self.doctor, [self.pattern], [blackout], date(2027, 3, 14)
            )

        self.assertEqual(created, 0)
        put_item.assert_not_called()


class DoctorApiTests(unittest.TestCase):
    def test_patient_role_cannot_use_doctor_route(self):
        result = app.lambda_handler(
            {
                "httpMethod": "GET",
                "pathParameters": {"proxy": "consultation-types"},
                "requestContext": {
                    "authorizer": {"role": "PATIENT", "userId": "25"}
                },
            },
            None,
        )

        self.assertEqual(result["statusCode"], 403)

    def test_health_camp_capacity_is_bounded(self):
        with self.assertRaises(ValueError):
            app.validate_consultation(
                {
                    "consultation_name": "Health Camp Session",
                    "duration_minutes": 60,
                    "capacity": 11,
                    "fee": "25.00",
                }
            )

    def test_fixed_consultation_capacity_cannot_be_changed(self):
        with self.assertRaises(ValueError):
            app.validate_consultation(
                {
                    "consultation_name": "Quick Consult",
                    "duration_minutes": 15,
                    "capacity": 2,
                    "fee": "25.00",
                }
            )


if __name__ == "__main__":
    unittest.main()