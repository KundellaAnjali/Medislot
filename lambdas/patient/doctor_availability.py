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


def get_connection():
    from profile import get_connection as connect

    return connect()


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


def get_slots(doctor_id, start_date, end_date, include_consultation_id=False):
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
            slot_details = {
                "slot_id": int(slot["slot_id"]),
                "start_time": slot["start_time"],
                "end_time": slot["end_time"],
                "consultation_type": slot.get("consultation_name", ""),
                "capacity": capacity,
                "booked_count": booked_count,
                "available_seats": capacity - booked_count,
            }
            if (
                include_consultation_id
                and slot.get("consultation_type_id") is not None
            ):
                slot_details["consultation_type_id"] = int(
                    slot["consultation_type_id"]
                )
            slots.append(slot_details)

        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query["ExclusiveStartKey"] = last_key

    slots.sort(key=lambda slot: slot["start_time"])
    return slots


def get_slots_for_date(slot_date, now):
    day_start = datetime.combine(
        slot_date, time.min, tzinfo=timezone.utc
    )
    day_end = datetime.combine(
        slot_date, time.max.replace(microsecond=0), tzinfo=timezone.utc
    )
    if slot_date == now.date():
        day_start = now

    query = {
        "IndexName": "LocalDateStartIndex",
        "KeyConditionExpression": Key("local_date").eq(slot_date.isoformat())
        & Key("start_time").between(
            day_start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            day_end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        ),
    }
    now_timestamp = int(now.timestamp())
    slots = []

    while True:
        result = slots_table.query(**query)
        for slot in result.get("Items", []):
            slot_start = datetime.fromisoformat(
                slot["start_time"].replace("Z", "+00:00")
            )
            if slot_start <= now or int(slot.get("status", 1)) != 1:
                continue
            if (
                slot.get("expires_at") is not None
                and int(slot["expires_at"]) <= now_timestamp
            ):
                continue

            capacity = int(slot["capacity"])
            booked_count = int(slot.get("booked_count", 0))
            available_seats = capacity - booked_count
            if available_seats <= 0:
                continue

            slots.append(
                {
                    "doctor_id": int(slot["doctor_id"]),
                    "consultation_type_id": int(slot["consultation_type_id"]),
                    "slot_id": int(slot["slot_id"]),
                    "local_date": slot.get("local_date", slot_date.isoformat()),
                    "start_time": slot["start_time"],
                    "end_time": slot["end_time"],
                    "capacity": capacity,
                    "booked_count": booked_count,
                    "available_seats": available_seats,
                }
            )

        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query["ExclusiveStartKey"] = last_key

    return slots


def get_verified_doctor_details(doctor_ids):
    if not doctor_ids:
        return {}

    placeholders = ", ".join(["%s"] * len(doctor_ids))
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT d.doctor_id,
                       CONCAT_WS(' ', NULLIF(d.first_name, ''),
                                 NULLIF(d.last_name, '')) AS doctor_name,
                       GROUP_CONCAT(
                           DISTINCT s.specialization_name
                           ORDER BY s.specialization_name SEPARATOR ', '
                       ) AS specialization,
                       h.city,
                       ct.consultation_type_id,
                       ct.consultation_name AS consultation_type,
                       dct.fee
                FROM doctors d
                JOIN users u ON u.user_id = d.user_id AND u.is_active = TRUE
                LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
                LEFT JOIN doctor_specializations ds
                  ON ds.doctor_id = d.doctor_id
                LEFT JOIN specializations s
                  ON s.specialization_id = ds.specialization_id
                JOIN doctor_consulting_types dct
                  ON dct.doctor_id = d.doctor_id AND dct.is_active = TRUE
                JOIN consultation_types ct
                  ON ct.consultation_type_id = dct.consultation_type_id
                WHERE d.is_verified = TRUE
                  AND d.doctor_id IN ({placeholders})
                GROUP BY d.doctor_id, d.first_name, d.last_name, h.city,
                         ct.consultation_type_id, ct.consultation_name, dct.fee
                """,
                doctor_ids,
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    return {
        (int(row["doctor_id"]), int(row["consultation_type_id"])): {
            "doctor_name": row["doctor_name"] or "Doctor",
            "specialization": row["specialization"] or "",
            "city": row["city"] or "",
            "consultation_type": row["consultation_type"],
            "fee": float(row["fee"]),
        }
        for row in rows
    }


def get_search_slots(start_date, end_date, doctor_id=None):
    if doctor_id is not None:
        slots = get_slots(
            doctor_id,
            start_date,
            end_date,
            include_consultation_id=True,
        )
        enriched_slots = [
            {
                **slot,
                "doctor_id": doctor_id,
                "local_date": slot["start_time"][:10],
            }
            for slot in slots
            if int(slot.get("available_seats", 0)) > 0
        ]
    else:
        now = datetime.now(timezone.utc)
        slots = []
        current_date = start_date
        while current_date <= end_date:
            if current_date >= now.date():
                slots.extend(get_slots_for_date(current_date, now))
            current_date = date.fromordinal(current_date.toordinal() + 1)
        enriched_slots = slots

    doctor_ids = sorted({slot["doctor_id"] for slot in enriched_slots})
    doctor_details = get_verified_doctor_details(doctor_ids)
    results = []
    for slot in enriched_slots:
        consultation_type_id = slot.get("consultation_type_id")
        if consultation_type_id is None:
            continue
        details = doctor_details.get(
            (slot["doctor_id"], int(consultation_type_id))
        )
        if details:
            results.append({**slot, **details})

    results.sort(key=lambda slot: (slot["local_date"], slot["start_time"]))
    return results


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    publish_metric("DoctorAvailabilityRequests", 1)

    authorizer = event.get("requestContext", {}).get("authorizer", {})
    if not authorizer.get("userId"):
        logger.warning(
            "Doctor availability rejected: missing authenticated identity request_id=%s",
            request_id,
        )
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(401, {"message": "Authentication is required."})
    if authorizer.get("role") != "PATIENT":
        logger.warning(
            "Doctor availability access denied: expected_role=PATIENT actual_role=%s request_id=%s",
            authorizer.get("role", "unknown"),
            request_id,
        )
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(
            403,
            {"message": "You are not allowed to view doctor availability."},
        )

    resource = event.get("resource", "")
    path = event.get("path", "").rstrip("/")
    aggregate_route = resource == "/doctors/availability" or path.endswith(
        "/doctors/availability"
    )

    try:
        doctor_id = None if aggregate_route else get_doctor_id(event)
        query_parameters = event.get("queryStringParameters") or {}
        if aggregate_route and query_parameters.get("doctor_id") is not None:
            doctor_id_value = {
                "pathParameters": {"doctor_id": query_parameters["doctor_id"]}
            }
            doctor_id = get_doctor_id(doctor_id_value)
        start_date = parse_date(query_parameters.get("start_date"), "start_date")
        end_date = parse_date(query_parameters.get("end_date"), "end_date")
        if end_date < start_date:
            raise AvailabilityValidationError(
                "end_date must be on or after start_date."
            )
    except AvailabilityValidationError as error:
        logger.warning(
            "Doctor availability request rejected: reason=%s request_id=%s",
            str(error),
            request_id,
        )
        publish_metric("DoctorAvailabilityErrors", 1)
        return response(400, {"message": str(error)})

    try:
        if aggregate_route:
            slots = get_search_slots(start_date, end_date, doctor_id)
        else:
            slots = get_slots(doctor_id, start_date, end_date)
        publish_metric("DoctorAvailabilitySuccesses", 1)
        publish_metric("DoctorAvailabilitySlotsReturned", len(slots))
        logger.info(
            "Doctor availability retrieved: scope=%s slots=%s request_id=%s",
            doctor_id if doctor_id is not None else "all_verified_doctors",
            len(slots),
            request_id,
        )
        return response(
            200,
            {
                **({"doctor_id": doctor_id} if doctor_id is not None else {}),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "slots": slots,
            },
        )
    except Exception:
        publish_metric("DoctorAvailabilityErrors", 1)
        logger.exception(
            "Doctor availability query failed: scope=%s request_id=%s",
            doctor_id if doctor_id is not None else "all_verified_doctors",
            request_id,
        )
        return response(500, {"message": "Could not load doctor availability."})
