import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from .appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )
    from .refund_service import RefundService, RefundServiceError
except ImportError:
    from appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )
    from refund_service import RefundService, RefundServiceError


logger = logging.getLogger()
logger.setLevel(logging.INFO)
SLOTS_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE_NAME = os.environ["IDEMPOTENCY_TABLE_NAME"]
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
ddb = boto3.resource("dynamodb")
ddb_client = boto3.client("dynamodb")
eventbridge = boto3.client("events")
slots_table = ddb.Table(SLOTS_TABLE_NAME)
IDEMPOTENCY_TTL_DAYS = 90
cloudwatch = boto3.client("cloudwatch")
SLOT_DEACTIVATION_RETRIES = 5


class CancellationError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class AppointmentCancellationService:
    def __init__(self, repository=None, refund_service=None):
        self.repository = repository or AppointmentRepository()
        self.refund_service = refund_service or RefundService()

    def cancel(self, role, user_id, appointment_id):
        if role not in {"PATIENT", "DOCTOR"}:
            raise CancellationError(403, "Only patients or doctors can cancel appointments.")
        try:
            result = self.repository.cancel_appointments(
                role=role,
                user_id=user_id,
                appointment_id=appointment_id,
                refund_service=self.refund_service,
                now=datetime.now(timezone.utc),
                deactivate_slot=deactivate_slot,
            )
        except RefundServiceError as error:
            logger.exception("Could not calculate appointment refund")
            raise CancellationError(
                503, "Refund processing is temporarily unavailable."
            ) from error
        except AppointmentRepositoryError as error:
            raise CancellationError(error.status_code, str(error)) from error
        cancelled_appointments = result.get("cancelled_appointments", [])
        for cancellation in cancelled_appointments:
            try:
                booked_count = release_cancelled_seat(cancellation)
                cancellation_event = {
                    key: value
                    for key, value in cancellation.items()
                    if key not in {"refund_tier", "_notification"}
                }
                cancellation_event["patient_email"] = cancellation[
                    "_notification"
                ]["email"]
                cancellation_event["patient_first_name"] = (
                    cancellation["_notification"]["first_name"] or ""
                )
                seat_event = {
                    "appointment_id": str(cancellation["appointment_id"]),
                    "doctor_id": str(cancellation["doctor_id"]),
                    "slot_id": str(cancellation["slot_id"]),
                    "slot_start_time": cancellation["slot_start_time"],
                    "booked_count": booked_count,
                    "status": "SEAT_FREED",
                }
                publish_cancellation_events(cancellation_event, seat_event)
            except CancellationError:
                raise
            except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as error:
                logger.exception(
                    "Could not release or publish cancellation for appointment_id=%s",
                    cancellation.get("appointment_id"),
                )
                raise CancellationError(
                    503, "Cancellation was saved, but follow-up processing failed."
                ) from error
            cancellation.pop("_notification", None)
        for tier_count in result.pop("tier_counts", []):
            try:
                cloudwatch.put_metric_data(
                    Namespace="MediSlot/Patient",
                    MetricData=[
                        {
                            "MetricName": "CancellationsByTier",
                            "Dimensions": [
                                {"Name": "Tier", "Value": tier_count["tier"]}
                            ],
                            "Value": tier_count["count"],
                            "Unit": "Count",
                        }
                    ],
                )
            except Exception:
                logger.exception(
                    "Could not publish cancellation metric tier=%s",
                    tier_count["tier"],
                )
        return result

    def mark_no_show(self, role, user_id, appointment_id):
        if role != "DOCTOR":
            raise CancellationError(403, "Only the assigned doctor can mark a no-show.")
        try:
            return self.repository.mark_no_show(
                user_id=user_id,
                appointment_id=appointment_id,
                refund_service=self.refund_service,
                now=datetime.now(timezone.utc),
            )
        except RefundServiceError as error:
            logger.exception("Could not calculate no-show refund")
            raise CancellationError(
                503, "Refund processing is temporarily unavailable."
            ) from error
        except AppointmentRepositoryError as error:
            raise CancellationError(error.status_code, str(error)) from error


def release_cancelled_seat(cancellation):
    appointment_id = str(cancellation["appointment_id"])
    marker_key = f"seat-release:{appointment_id}"
    now = datetime.now(timezone.utc)
    try:
        ddb_client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": IDEMPOTENCY_TABLE_NAME,
                        "Item": {
                            "idempotency_key": {"S": marker_key},
                            "appointment_id": {"N": appointment_id},
                            "created_at": {"S": now.isoformat()},
                            "expires_at": {
                                "N": str(
                                    int(
                                        (
                                            now
                                            + timedelta(days=IDEMPOTENCY_TTL_DAYS)
                                        ).timestamp()
                                    )
                                )
                            },
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(idempotency_key)"
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": SLOTS_TABLE_NAME,
                        "Key": {
                            "doctor_id": {
                                "N": str(cancellation["doctor_id"])
                            },
                            "slot_start_time": {
                                "S": slot_key_value(
                                    cancellation["slot_start_time"]
                                )
                            },
                        },
                        "UpdateExpression": (
                            "SET booked_count = booked_count - :one, "
                            "version = version + :one"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(doctor_id) "
                            "AND attribute_exists(slot_start_time) "
                            "AND booked_count >= :one"
                        ),
                        "ExpressionAttributeValues": {
                            ":one": {"N": "1"}
                        },
                    }
                },
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != (
            "TransactionCanceledException"
        ):
            raise
        marker = ddb_client.get_item(
            TableName=IDEMPOTENCY_TABLE_NAME,
            Key={"idempotency_key": {"S": marker_key}},
            ConsistentRead=True,
        )
        if not marker.get("Item"):
            raise

    slot = slots_table.get_item(
        Key={
            "doctor_id": int(cancellation["doctor_id"]),
            "slot_start_time": slot_key_value(cancellation["slot_start_time"]),
        },
        ConsistentRead=True,
    ).get("Item")
    if not slot:
        raise CancellationError(
            503, "The cancelled appointment slot could not be located."
        )
    return int(slot["booked_count"])


def publish_cancellation_events(cancellation_event, seat_event):
    result = eventbridge.put_events(
        Entries=[
            {
                "EventBusName": EVENT_BUS_NAME,
                "Source": "medislot.appointments",
                "DetailType": "AppointmentCancelled",
                "Detail": json.dumps(cancellation_event, separators=(",", ":")),
            },
            {
                "EventBusName": EVENT_BUS_NAME,
                "Source": "medislot.appointments",
                "DetailType": "SlotSeatFreed",
                "Detail": json.dumps(seat_event, separators=(",", ":")),
            },
        ]
    )
    entries = result.get("Entries", [])
    if (
        result.get("FailedEntryCount", 0)
        or len(entries) != 2
        or any(not entry.get("EventId") for entry in entries)
    ):
        raise CancellationError(
            503, "Cancellation was saved, but its events could not be published."
        )


def deactivate_slot(doctor_id, slot_start_time):
    for _ in range(SLOT_DEACTIVATION_RETRIES):
        result = slots_table.get_item(
            Key={
                "doctor_id": doctor_id,
                "slot_start_time": slot_key_value(slot_start_time),
            },
            ConsistentRead=True,
        )
        slot = result.get("Item")
        if not slot:
            raise AppointmentRepositoryError(
                503, "The appointment slot could not be located for cancellation."
            )
        if int(slot.get("status", 0)) != 1:
            return
        try:
            slots_table.update_item(
                Key={
                    "doctor_id": doctor_id,
                    "slot_start_time": slot_key_value(slot_start_time),
                },
                UpdateExpression="SET #status = :inactive, version = version + :one",
                ConditionExpression="#status = :active AND version = :version",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":inactive": 0,
                    ":active": 1,
                    ":version": int(slot["version"]),
                    ":one": 1,
                },
            )
            return
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
    raise AppointmentRepositoryError(
        409, "The slot changed repeatedly. Retry the cancellation."
    )


def slot_key_value(value):
    if isinstance(value, datetime):
        slot_start = value
    else:
        slot_start = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=timezone.utc)
    slot_start = slot_start.astimezone(timezone.utc)
    precision = "microseconds" if slot_start.microsecond else "seconds"
    return slot_start.isoformat(timespec=precision).replace("+00:00", "Z")
