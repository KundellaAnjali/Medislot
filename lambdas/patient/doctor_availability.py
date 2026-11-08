import json
import logging
import os
import re
from datetime import date, datetime, time, timezone

import boto3
from boto3.dynamodb.conditions import Key


logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "MediSlot/Patient"

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
slots_table = dynamodb.Table(os.environ["SLOTS_TABLE_NAME"])


class AvailabilityValidationError(Exception):
    pass


def publish_metric(metric_name, value, unit="Count"):
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                }
            ],
        )
    except Exception:
        logger.exception("Could not publish CloudWatch metric %s", metric_name)


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def parse_date(value, field):
    if not isinstance(value, str):
        raise AvailabilityValidationError(f"{field} must use YYYY-MM-DD format.")
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError as error:
        raise AvailabilityValidationError(
            f"{field} must use YYYY-MM-DD format."
        ) from error
    if parsed_date.isoformat() != value:
        raise AvailabilityValidationError(f"{field} must use YYYY-MM-DD format.")
    return parsed_date


def get_doctor_id(event):
    value = (event.get("pathParameters") or {}).get("doctor_id")
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value):
        raise AvailabilityValidationError("doctor_id must be a positive integer.")
    doctor_id = int(value)
    if not 1 <= doctor_id <= 2**31 - 1:
        raise AvailabilityValidationError("doctor_id must be a positive integer.")
    return doctor_id


def get_slots(doctor_id, start_date, end_date):
    start_time = datetime.combine(
        start_date, time.min, tzinfo=timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    end_time = datetime.combine(
        end_date, time.max.replace(microsecond=0), tzinfo=timezone.utc
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    now = datetime.now(timezone.utc)
    now_timestamp = int(now.timestamp())
    slots = []
    query = {
        "IndexName": "DoctorStartIndex",
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id)
        & Key("start_time").between(start_time, end_time),
    }

    while True:
        result = slots_table.query(**query)
        for slot in result.get("Items", []):
            slot_start = datetime.fromisoformat(
                slot["start_time"].replace("Z", "+00:00")
            )
            if slot_start <= now:
                continue
            if int(slot.get("status", 1)) != 1:
                continue
            if (
                slot.get("expires_at") is not None
                and int(slot["expires_at"]) <= now_timestamp
            ):
                continue

            capacity = int(slot["capacity"])
            booked_count = int(slot.get("booked_count", 0))
            slots.append(
                {
                    "slot_id": int(slot["slot_id"]),
                    "start_time": slot["start_time"],
                    "end_time": slot["end_time"],
                    "consultation_type": slot.get("consultation_name", ""),
                    "capacity": capacity,
                    "booked_count": booked_count,
                    "available_seats": capacity - booked_count,
                }
            )

        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query["ExclusiveStartKey"] = last_key

    slots.sort(key=lambda slot: slot["start_time"])
    return slots


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    publish_metric("DoctorAvailabilityRequests", 1)

    authorizer = event.get("requestContext", {}).get("authorizer", {})
    if not authorizer.get("userId"):
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(401, {"message": "Authentication is required."})
    if authorizer.get("role") != "PATIENT":
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(
            403,
            {"message": "You are not allowed to view doctor availability."},
        )

    try:
        doctor_id = get_doctor_id(event)
        query_parameters = event.get("queryStringParameters") or {}
        start_date = parse_date(query_parameters.get("start_date"), "start_date")
        end_date = parse_date(query_parameters.get("end_date"), "end_date")
        if end_date < start_date:
            raise AvailabilityValidationError(
                "end_date must be on or after start_date."
            )
    except AvailabilityValidationError as error:
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(400, {"message": str(error)})

    try:
        slots = get_slots(doctor_id, start_date, end_date)
        publish_metric("DoctorAvailabilitySuccesses", 1)
        publish_metric("DoctorAvailabilitySlotsReturned", len(slots))
        logger.info(
            "Doctor availability retrieved: doctor_id=%s slots=%s request_id=%s",
            doctor_id,
            len(slots),
            request_id,
        )
        return response(
            200,
            {
                "doctor_id": doctor_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "slots": slots,
            },
        )
    except Exception:
        publish_metric("DoctorAvailabilityErrors", 1)
        logger.exception(
            "Doctor availability query failed: doctor_id=%s request_id=%s",
            doctor_id,
            request_id,
        )
        return response(500, {"message": "Could not load doctor availability."})
