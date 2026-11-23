import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from .appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )
except ImportError:
    from appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )


logger = logging.getLogger()
logger.setLevel(logging.INFO)
ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
SLOTS_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE_NAME = os.environ["IDEMPOTENCY_TABLE_NAME"]
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
ddb_client = boto3.client("dynamodb")
eventbridge = boto3.client("events")
slots_table = ddb.Table(SLOTS_TABLE_NAME)
IDEMPOTENCY_TTL_DAYS = 90
cloudwatch = boto3.client("cloudwatch")
parameter_cache = {}
SLOT_DEACTIVATION_RETRIES = 5


class CancellationError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def get_refund_configuration():
    try:
        config = {
            "full_refund_hours": int(
                get_parameter_value("FULL_REFUND_HOURS")
            ),
            "partial_refund_hours": int(
                get_parameter_value("PARTIAL_REFUND_HOURS")
            ),
            "partial_refund_percent": Decimal(
                get_parameter_value("PARTIAL_REFUND_PERCENT")
            ),
        }
    except (
        ClientError,
        DecimalException,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        logger.exception("Invalid appointment refund configuration in SSM")
        raise CancellationError(
            503, "Refund processing is temporarily unavailable."
        ) from error
    if (
        config["full_refund_hours"] <= config["partial_refund_hours"]
        or config["partial_refund_hours"] < 0
        or config["partial_refund_percent"] < 0
        or config["partial_refund_percent"] > 100
    ):
        raise CancellationError(
            503, "Refund processing is temporarily unavailable."
        )
    return config



def get_parameter_value(name):
    if name not in parameter_cache:
        result = ssm.get_parameter(
            Name=f"/medislot/{ENVIRONMENT}/{name}"
        )
        parameter_cache[name] = result["Parameter"]["Value"]
    return parameter_cache[name]


def calculate_refund(appointment_time, paid_amount, config, doctor_cancel=False, now=None):
    if doctor_cancel:
        percentage = Decimal("100")
        tier = "FULL"
        reason = "DOCTOR_SLOT_CANCELLATION"
    else:
        current_time = now or datetime.now(timezone.utc)
        if appointment_time.tzinfo is None:
            appointment_time = appointment_time.replace(tzinfo=timezone.utc)
        time_until = (
            appointment_time.astimezone(timezone.utc)
            - current_time.astimezone(timezone.utc)
        )
        if time_until > timedelta(hours=config["full_refund_hours"]):
            percentage = Decimal("100")
            tier = "FULL"
            reason = "PATIENT_CANCELLATION_FULL_REFUND"
        elif time_until >= timedelta(
            hours=config["partial_refund_hours"]
        ):
            percentage = config["partial_refund_percent"]
            tier = "PARTIAL"
            reason = "PATIENT_CANCELLATION_PARTIAL_REFUND"
        else:
            percentage = Decimal("0")
            tier = "NONE"
            reason = "PATIENT_CANCELLATION_NO_REFUND"

    amount = (
        Decimal(str(paid_amount)) * percentage / Decimal("100")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "refund_amount": amount,
        "refund_percentage": percentage,
        "refund_tier": tier,
        "refund_reason": reason,
    }


class AppointmentCancellationService:
    def __init__(self, repository=None):
        self.repository = repository or AppointmentRepository()

    def cancel(self, role, user_id, appointment_id):
        if role not in {"PATIENT", "DOCTOR"}:
            raise CancellationError(403, "Only patients or doctors can cancel appointments.")
        config = get_refund_configuration()
        try:
            result = self.repository.cancel_appointments(
                role=role,
                user_id=user_id,
                appointment_id=appointment_id,
                refund_config=config,
                refund_calculator=calculate_refund,
                now=datetime.now(timezone.utc),
                deactivate_slot=deactivate_slot,
            )
        except AppointmentRepositoryError as error:
            raise CancellationError(error.status_code, str(error)) from error
        cancelled_appointments = result.get("cancelled_appointments", [])
        for cancellation in cancelled_appointments:
            try:
                booked_count = release_cancelled_seat(cancellation)
                cancellation_event = {
                    key: value
                    for key, value in cancellation.items()
                    if key not in {"refund_tier"}
                }
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
                            "ttl": {
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
