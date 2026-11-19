import json
import logging
import os
import re
from datetime import date, datetime, time, timezone

import boto3
from boto3.dynamodb.conditions import Key

from profile import get_connection


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


def get_slots(doctor_id, start_date, end_date, consultation_type=None):
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
                consultation_type is not None
                and slot.get("consultation_name") != consultation_type
            ):
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
                    "doctor_id": int(slot["doctor_id"]),
                    "consultation_type_id": int(slot["consultation_type_id"]),
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


def get_verified_doctors(cursor):
    cursor.execute(
        """
        SELECT
            d.doctor_id,
            TRIM(CONCAT_WS(' ', d.first_name, d.last_name)) AS doctor_name,
            h.hospital_name,
            GROUP_CONCAT(
                DISTINCT s.specialization_name
                ORDER BY s.specialization_name
                SEPARATOR ', '
            ) AS specialization,
            h.city
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s
          ON s.specialization_id = ds.specialization_id
        WHERE d.is_verified = TRUE
        GROUP BY d.doctor_id, d.first_name, d.last_name,
                 h.hospital_name, h.city
        ORDER BY d.doctor_id
        """
    )
    return cursor.fetchall()


def get_consultation_fees(cursor):
    cursor.execute(
        """
        SELECT
            dct.doctor_id,
            dct.consultation_type_id,
            dct.fee
        FROM doctor_consulting_types dct
        """
    )
    fees = {}
    for row in cursor.fetchall():
        fees.setdefault(int(row["doctor_id"]), {})[
            int(row["consultation_type_id"])
        ] = row["fee"]
    return fees


def get_doctor_details(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            d.doctor_id,
            TRIM(CONCAT_WS(' ', d.first_name, d.last_name)) AS doctor_name,
            h.hospital_name,
            h.city,
            GROUP_CONCAT(
                DISTINCT s.specialization_name
                ORDER BY s.specialization_name
                SEPARATOR ', '
            ) AS specialization
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s
          ON s.specialization_id = ds.specialization_id
        WHERE d.doctor_id = %s
        GROUP BY d.doctor_id, d.first_name, d.last_name,
                 h.hospital_name, h.city
        """,
        (doctor_id,),
    )
    return cursor.fetchone()


def get_available_consultation_types(cursor):
    cursor.execute(
        """
        SELECT DISTINCT ct.consultation_name
        FROM consultation_types ct
        ORDER BY ct.consultation_name
        """
    )
    return [row["consultation_name"] for row in cursor.fetchall()]


def get_verified_doctor_slots(start_date, end_date, consultation_type=None):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            doctors = get_verified_doctors(cursor)
            fees_by_doctor = get_consultation_fees(cursor)
            consultation_types = get_available_consultation_types(cursor)

        if (
            consultation_type is not None
            and consultation_type not in consultation_types
        ):
            raise AvailabilityValidationError(
                "consultation_type must be an available consultation type."
            )

        slots = []
        for doctor in doctors:
            doctor_id = int(doctor["doctor_id"])
            slots.extend(
                enrich_doctor_slots(
                    get_slots(
                        doctor_id, start_date, end_date, consultation_type
                    ),
                    doctor,
                    fees_by_doctor,
                )
            )
        slots.sort(key=lambda slot: (slot["start_time"], slot["doctor_id"]))
        return slots
    finally:
        connection.close()


def enrich_doctor_slots(slots, doctor, fees):
    doctor_id = int(doctor["doctor_id"])
    doctor_fees = fees.get(doctor_id, {})
    for slot in slots:
        fee = doctor_fees.get(slot["consultation_type_id"])
        slot.update(
            {
                "doctor_id": doctor_id,
                "doctor_name": doctor["doctor_name"],
                "hospital_name": doctor["hospital_name"],
                "specialization": doctor["specialization"],
                "city": doctor["city"],
                "consultation_fee": str(fee) if fee is not None else None,
            }
        )
    return slots


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
    is_types_route = (
        resource == "/doctors/availability/types"
        or path.endswith("/doctors/availability/types")
    )
    is_search_route = (
        resource == "/doctors/availability/search"
        or path.endswith("/doctors/availability/search")
    )

    try:
        doctor_id = None if is_search_route or is_types_route else get_doctor_id(event)
        query_parameters = event.get("queryStringParameters") or {}
        consultation_type = query_parameters.get("consultation_type") or None
        if consultation_type is not None and (
            not isinstance(consultation_type, str)
            or not consultation_type.strip()
            or len(consultation_type) > 255
            or consultation_type != consultation_type.strip()
        ):
            raise AvailabilityValidationError(
                "consultation_type must be a valid consultation type."
            )
        if not is_types_route:
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
        if is_types_route:
            connection = get_connection()
            try:
                with connection.cursor() as cursor:
                    consultation_types = get_available_consultation_types(cursor)
            finally:
                connection.close()
            return response(200, {"consultation_types": consultation_types})
        if is_search_route:
            slots = get_verified_doctor_slots(
                start_date, end_date, consultation_type
            )
        else:
            if consultation_type is not None:
                connection = get_connection()
                try:
                    with connection.cursor() as cursor:
                        consultation_types = get_available_consultation_types(cursor)
                finally:
                    connection.close()
                if consultation_type not in consultation_types:
                    raise AvailabilityValidationError(
                        "consultation_type must be an available consultation type."
                    )
            slots = get_slots(
                doctor_id, start_date, end_date, consultation_type
            )
            connection = get_connection()
            try:
                with connection.cursor() as cursor:
                    doctor = get_doctor_details(cursor, doctor_id)
                    fees = get_consultation_fees(cursor)
            finally:
                connection.close()
            if not doctor:
                raise AvailabilityValidationError("Doctor not found.")
            slots = enrich_doctor_slots(slots, doctor, fees)
        publish_metric("DoctorAvailabilitySuccesses", 1)
        publish_metric("DoctorAvailabilitySlotsReturned", len(slots))
        if is_search_route:
            logger.info(
                "Verified doctor availability search completed: slots=%s request_id=%s",
                len(slots),
                request_id,
            )
        else:
            logger.info(
                "Doctor availability retrieved: doctor_id=%s slots=%s request_id=%s",
                doctor_id,
                len(slots),
                request_id,
            )
        return response(
            200,
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "slots": slots,
                **({"doctor_id": doctor_id} if doctor_id is not None else {}),
                **(
                    {"consultation_type": consultation_type}
                    if consultation_type is not None
                    else {}
                ),
            },
        )
    except AvailabilityValidationError as error:
        publish_metric("DoctorAvailabilityErrors", 1)
        logger.warning(
            "Doctor availability request rejected: reason=%s request_id=%s",
            str(error),
            request_id,
        )
        return response(400, {"message": str(error)})
    except Exception:
        publish_metric("DoctorAvailabilityErrors", 1)
        if is_search_route:
            logger.exception(
                "Verified doctor availability search failed: request_id=%s",
                request_id,
            )
        else:
            logger.exception(
                "Doctor availability query failed: doctor_id=%s request_id=%s",
                doctor_id,
                request_id,
            )
        return response(500, {"message": "Could not load doctor availability."})
