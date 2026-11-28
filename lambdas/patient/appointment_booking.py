import base64
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from .appointment_repository import AppointmentRepository
    from .appointment_service import (
        AppointmentService,
        AppointmentServiceError,
    )
except ImportError:
    from appointment_repository import AppointmentRepository
    from appointment_service import AppointmentService, AppointmentServiceError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

SLOTS_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE_NAME = os.environ["IDEMPOTENCY_TABLE_NAME"]
ENVIRONMENT = os.environ.get("ENVIRONMENT_NAME", "").lower()
RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
UTC_SLOT_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
slots_table = dynamodb.Table(SLOTS_TABLE_NAME)
idempotency_table = dynamodb.Table(IDEMPOTENCY_TABLE_NAME)


class BookingError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def publish_metric(metric_name, value=1):
    try:
        cloudwatch.put_metric_data(
            Namespace="MediSlot/Patient",
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logger.exception("Could not publish booking metric %s", metric_name)


PAYMENT_METHODS = {
    "CREDIT_CARD",
    "DEBIT_CARD",
    "UPI",
    "NET_BANKING",
    "WALLET",
    "CASH",
}
PAYMENT_OUTCOMES = {"SUCCESS", "FAILED"}


def parse_booking_request(event):
    raw_body = event.get("body")
    if event.get("isBase64Encoded") and raw_body:
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise BookingError(400, "Request body must be valid JSON.") from error

    try:
        body = json.loads(raw_body) if raw_body else {}
    except (TypeError, json.JSONDecodeError) as error:
        raise BookingError(400, "Request body must be valid JSON.") from error
    if not isinstance(body, dict):
        raise BookingError(400, "Request body must be a JSON object.")
    allowed = {
        "doctor_id",
        "slot_start_time",
        "payment_method",
        "test_payment_outcome",
    }
    if not {"doctor_id", "slot_start_time"}.issubset(body) or set(body) - allowed:
        raise BookingError(
            400,
            "Request must contain doctor_id and slot_start_time, with optional payment_method and test_payment_outcome.",
        )

    doctor_value = body["doctor_id"]
    if isinstance(doctor_value, bool) or not re.fullmatch(
        r"[1-9][0-9]{0,9}", str(doctor_value)
    ):
        raise BookingError(400, "doctor_id must be a positive integer.")
    doctor_id = int(doctor_value)
    if doctor_id > 2**31 - 1:
        raise BookingError(400, "doctor_id must be a positive integer.")

    value = body["slot_start_time"]
    if not isinstance(value, str) or not UTC_SLOT_TIME_PATTERN.fullmatch(value):
        raise BookingError(
            400,
            "slot_start_time must be a UTC ISO-8601 timestamp.",
        )
    try:
        slot_start = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BookingError(
            400,
            "slot_start_time must be a valid UTC ISO-8601 timestamp.",
        ) from error
    if slot_start.utcoffset() != timezone.utc.utcoffset(slot_start):
        raise BookingError(400, "slot_start_time must be in UTC.")
    timespec = "microseconds" if slot_start.microsecond else "seconds"
    slot_start_time = (
        slot_start.astimezone(timezone.utc)
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )
    payment_method = body.get("payment_method", "UPI")
    if (
        not isinstance(payment_method, str)
        or payment_method.upper() not in PAYMENT_METHODS
    ):
        raise BookingError(
            400,
            f"payment_method must be one of: {', '.join(sorted(PAYMENT_METHODS))}.",
        )
    payment_outcome = body.get("test_payment_outcome", "SUCCESS")
    if (
        not isinstance(payment_outcome, str)
        or payment_outcome.upper() not in PAYMENT_OUTCOMES
    ):
        raise BookingError(
            400,
            "test_payment_outcome must be SUCCESS or FAILED.",
        )
    if "test_payment_outcome" in body and ENVIRONMENT.startswith(("prod", "live")):
        raise BookingError(
            400,
            "test_payment_outcome is available only in non-production environments.",
        )
    return (
        doctor_id,
        slot_start_time,
        payment_method.upper(),
        payment_outcome.upper(),
    )


def get_slot(doctor_id, slot_start_time):
    result = slots_table.get_item(
        Key={
            "doctor_id": doctor_id,
            "slot_start_time": slot_start_time,
        },
        ConsistentRead=True,
    )
    return result.get("Item")


def validate_slot(slot):
    now = datetime.now(timezone.utc)
    try:
        slot_start = datetime.fromisoformat(
            slot["slot_start_time"].replace("Z", "+00:00")
        )
        if slot_start.tzinfo is None:
            raise ValueError("Slot start time has no timezone.")
        capacity = int(slot["capacity"])
        booked_count = int(slot["booked_count"])
        version = int(slot["version"])
        status = int(slot["status"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Slot record is missing required booking fields.") from error

    if status != 1:
        raise BookingError(409, "This appointment slot is not active.")
    if slot_start <= now:
        raise BookingError(409, "This appointment slot is in the past.")
    if (
        capacity < 1
        or booked_count < 0
        or version < 0
    ):
        raise RuntimeError("Slot record contains invalid booking values.")
    if booked_count >= capacity:
        raise BookingError(409, "This appointment slot is fully booked.")
    reservation_cutoff = now.replace(microsecond=0)
    if now.microsecond:
        reservation_cutoff += timedelta(seconds=1)
    return capacity, booked_count, version, reservation_cutoff


def reserve_slot(doctor_id, slot_start_time, slot):
    for retry_index in range(len(RETRY_DELAYS) + 1):
        if slot is None:
            raise BookingError(404, "Appointment slot was not found.")
        _, booked_count, version, now = validate_slot(slot)
        try:
            result = slots_table.update_item(
                Key={
                    "doctor_id": doctor_id,
                    "slot_start_time": slot_start_time,
                },
                UpdateExpression=(
                    "SET booked_count = booked_count + :one, "
                    "version = version + :one"
                ),
                ConditionExpression=(
                    "attribute_exists(doctor_id) "
                    "AND attribute_exists(slot_start_time) "
                    "AND #status = :active "
                    "AND booked_count = :booked_count "
                    "AND version = :version "
                    "AND booked_count < #capacity "
                    "AND slot_start_time > :now"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#capacity": "capacity",
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":active": 1,
                    ":booked_count": booked_count,
                    ":version": version,
                    ":now": now.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    ),
                },
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
            if retry_index == len(RETRY_DELAYS):
                raise BookingError(
                    409,
                    "This slot changed while you were booking. Please try again.",
                ) from error
            publish_metric("BookingConflictRetries")
            time.sleep(RETRY_DELAYS[retry_index])
            slot = get_slot(doctor_id, slot_start_time)
    raise RuntimeError("Booking retry loop ended unexpectedly.")


def release_slot_reservation(doctor_id, slot_start_time):
    try:
        slots_table.update_item(
            Key={
                "doctor_id": doctor_id,
                "slot_start_time": slot_start_time,
            },
            UpdateExpression=(
                "SET booked_count = booked_count - :one, "
                "version = version + :one"
            ),
            ConditionExpression=(
                "attribute_exists(doctor_id) "
                "AND attribute_exists(version) "
                "AND booked_count >= :one"
            ),
            ExpressionAttributeValues={":one": 1},
        )
    except Exception:
        logger.exception(
            "Could not release slot reservation after appointment persistence failed: "
            "doctor_id=%s slot_start_time=%s",
            doctor_id,
            slot_start_time,
        )
        raise


def create_booking(
    user_id,
    idempotency_key,
    doctor_id,
    slot_start_time,
    payment_method,
    payment_outcome,
):
    service = AppointmentService(
        repository=AppointmentRepository(),
        idempotency_table=idempotency_table,
        get_slot=get_slot,
        validate_slot=validate_slot,
        reserve_slot=reserve_slot,
        release_slot=release_slot_reservation,
    )
    try:
        return service.book(
            user_id,
            idempotency_key,
            doctor_id,
            slot_start_time,
            payment_method,
            payment_outcome,
        )
    except AppointmentServiceError as error:
        raise BookingError(error.status_code, str(error)) from error


def get_idempotency_key(event):
    headers = event.get("headers") or {}
    value = next(
        (
            header_value
            for name, header_value in headers.items()
            if isinstance(name, str) and name.lower() == "idempotency-key"
        ),
        None,
    )
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 255
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise BookingError(
            400,
            "A valid Idempotency-Key header (8-255 non-space ASCII characters) is required.",
        )
    return value


def lambda_handler(event, context):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    if not authorizer.get("userId"):
        publish_metric("FailedBookings")
        return response(401, {"message": "Authentication is required."})
    if authorizer.get("role") != "PATIENT":
        publish_metric("FailedBookings")
        return response(
            403,
            {"message": "Only patients can book appointments."},
        )
    if event.get("httpMethod", "").upper() != "POST" or not (
        event.get("resource") == "/appointments/book"
        or event.get("path", "").rstrip("/").endswith("/appointments/book")
    ):
        publish_metric("FailedBookings")
        return response(404, {"message": "Route not found."})

    try:
        user_id = int(authorizer["userId"])
        if user_id <= 0 or user_id > 2**31 - 1:
            raise ValueError
    except (TypeError, ValueError):
        publish_metric("FailedBookings")
        return response(401, {"message": "Authentication is required."})

    try:
        idempotency_key = get_idempotency_key(event)
        (
            doctor_id,
            slot_start_time,
            payment_method,
            payment_outcome,
        ) = parse_booking_request(event)
        result, created = create_booking(
            user_id,
            idempotency_key,
            doctor_id,
            slot_start_time,
            payment_method,
            payment_outcome,
        )
        if result.get("payment_status", "SUCCESS") == "FAILED":
            publish_metric("FailedBookings")
            logger.info(
                "Test payment failed: patient_user_id=%s doctor_id=%s "
                "appointment_id=%s transaction_id=%s",
                user_id,
                doctor_id,
                result["appointment_id"],
                result["transaction_id"],
            )
            return response(402, result)
        publish_metric("SuccessfulBookings")
        logger.info(
            "Appointment booked: patient_user_id=%s doctor_id=%s "
            "appointment_id=%s payment_method=%s transaction_id=%s "
            "created=%s",
            user_id,
            doctor_id,
            result["appointment_id"],
            result["payment_method"],
            result["transaction_id"],
            created,
        )
        return response(201, result)
    except BookingError as error:
        publish_metric("FailedBookings")
        return response(error.status_code, {"message": str(error)})
    except Exception:
        publish_metric("FailedBookings")
        logger.exception("Appointment booking failed")
        return response(500, {"message": "Could not book this appointment."})
