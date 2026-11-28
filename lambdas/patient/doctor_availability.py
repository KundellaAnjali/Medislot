import json
import logging
import os
import re
from datetime import date, datetime, time, timezone

import boto3
import pymysql
from slot_queries import query_slots_in_range


logger = logging.getLogger()
logger.setLevel(logging.INFO)

METRIC_NAMESPACE = "MediSlot/Patient"
ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
ssm = boto3.client("ssm")
slots_table = dynamodb.Table(os.environ["SLOTS_TABLE_NAME"])
parameter_cache = {}


class AvailabilityValidationError(Exception):
    pass


def get_parameter(name, decrypt=False):
    if name not in parameter_cache:
        result = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        parameter_cache[name] = result["Parameter"]["Value"]
    return parameter_cache[name]


def get_connection():
    return pymysql.connect(
        host=get_parameter(f"/medislot/{ENVIRONMENT}/rds-host"),
        port=int(get_parameter(f"/medislot/{ENVIRONMENT}/rds-port")),
        user=get_parameter(f"/medislot/{ENVIRONMENT}/rds-user"),
        password=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-password",
            decrypt=True,
        ),
        database=get_parameter(f"/medislot/{ENVIRONMENT}/rds-database"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


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
    slots = []
    for slot in query_slots_in_range(
        slots_table,
        doctor_id,
        start_time,
        end_time,
    ):
        slot_start = datetime.fromisoformat(
            slot["slot_start_time"].replace("Z", "+00:00")
        )
        if slot_start <= now:
            continue
        if int(slot.get("status", 1)) != 1:
            continue
        capacity = int(slot["capacity"])
        booked_count = int(slot.get("booked_count", 0))
        slots.append(
            {
                "slot_id": int(slot["slot_id"]),
                "doctor_id": int(slot["doctor_id"]),
                "consultation_type_id": int(slot["consultation_type_id"]),
                "start_time": slot["slot_start_time"],
                "end_time": slot["slot_end_time"],
                "capacity": capacity,
                "booked_count": booked_count,
                "available_seats": max(capacity - booked_count, 0),
                "is_full": booked_count >= capacity,
            }
        )

    slots.sort(key=lambda slot: slot["start_time"])
    return slots


def consultation_names_by_id(consultation_type_ids):
    if not consultation_type_ids:
        return {}
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(consultation_type_ids))
            cursor.execute(
                f"""
                SELECT consultation_type_id, consultation_name
                FROM consultation_types
                WHERE consultation_type_id IN ({placeholders})
                """,
                tuple(consultation_type_ids),
            )
            return {
                int(row["consultation_type_id"]): row["consultation_name"]
                for row in cursor.fetchall()
            }
    finally:
        connection.close()


def patient_booked_slot_ids(user_id, doctor_id, slots):
    if not slots:
        return set()
    placeholders = ",".join(["%s"] * len(slots))
    slot_ids = [int(slot["slot_id"]) for slot in slots]
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT DISTINCT a.slot_id
                FROM appointments a
                JOIN patients p ON p.patient_id = a.patient_id
                WHERE p.user_id = %s
                  AND a.doctor_id = %s
                  AND a.slot_id IN ({placeholders})
                  AND a.status = 'BOOKED'
                  AND a.appointment_status = 'CONFIRMED'
                """,
                (user_id, doctor_id, *slot_ids),
            )
            return {int(row["slot_id"]) for row in cursor.fetchall()}
    finally:
        connection.close()


def lambda_handler(event, context):
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
        booked_slot_ids = patient_booked_slot_ids(
            int(authorizer["userId"]),
            doctor_id,
            slots,
        )
        consultation_names = consultation_names_by_id(
            {slot["consultation_type_id"] for slot in slots}
        )
        for slot in slots:
            slot["consultation_type"] = consultation_names.get(
                slot["consultation_type_id"],
                "Consultation",
            )
            slot["patient_has_active_booking"] = (
                slot["slot_id"] in booked_slot_ids
            )
        publish_metric("DoctorAvailabilitySuccesses", 1)
        publish_metric("DoctorAvailabilitySlotsReturned", len(slots))
        logger.info(
            "Doctor availability retrieved: doctor_id=%s slots=%s",
            doctor_id,
            len(slots),
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
            "Doctor availability query failed: doctor_id=%s",
            doctor_id,
        )
        return response(500, {"message": "Could not load doctor availability."})
