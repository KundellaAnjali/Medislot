import hashlib
from datetime import date, datetime, timedelta, timezone
from time import perf_counter

from botocore.exceptions import ClientError

import os
import logging

import boto3
import pymysql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
SLOT_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]

ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")
dynamodb = boto3.resource("dynamodb")

slots_table = dynamodb.Table(SLOT_TABLE_NAME)

parameter_cache = {}
METRIC_NAMESPACE = "MediSlot/Doctor"


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
        logger.exception(
            "Could not publish CloudWatch metric %s",
            metric_name,
        )


def get_parameter(name):
    if name not in parameter_cache:
        response = ssm.get_parameter(Name=name)
        parameter_cache[name] = response["Parameter"]["Value"]
    return parameter_cache[name]


def get_connection():
    return pymysql.connect(
        host=get_parameter(f"/medislot/{ENVIRONMENT}/rds-host"),
        port=int(get_parameter(f"/medislot/{ENVIRONMENT}/rds-port")),
        user=get_parameter(f"/medislot/{ENVIRONMENT}/rds-user"),
        password=ssm.get_parameter(
            Name=f"/medislot/{ENVIRONMENT}/rds-password",
            WithDecryption=True,
        )["Parameter"]["Value"],
        database=get_parameter(f"/medislot/{ENVIRONMENT}/rds-database"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def parse_sql_time(value):
    from datetime import time

    if isinstance(value, time):
        return value.replace(tzinfo=None)

    seconds = int(value.total_seconds())
    return (datetime.min + timedelta(seconds=seconds)).time()


def utc_iso(value):
    return value.astimezone(
        timezone.utc
    ).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def weekday_name(value):
    name = str(value).upper()
    if name.isdigit():
        return [
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
            "SUNDAY",
        ][int(name)]
    return name


def build_slot_id(doctor_id, consultation_id, start_time):
    identity = f"{doctor_id}|{consultation_id}|{start_time}".encode("utf-8")
    return int(hashlib.sha256(identity).hexdigest()[:15], 16)



def materialize_for_doctor_day(
    doctor,
    patterns,
    blackout_rows,
    local_day
):
    created = 0

    for pattern in patterns:

        if weekday_name(pattern["day_of_week"]) != local_day.strftime("%A").upper():
            continue

        start_time = parse_sql_time(pattern["start_time"])
        end_time = parse_sql_time(pattern["end_time"])

        duration = int(pattern["duration_minutes"])

        if duration <= 0:
            raise ValueError("Slot duration must be greater than zero")

        # Original availability boundaries
        availability_start = datetime.combine(
            local_day,
            start_time,
            tzinfo=timezone.utc
        )

        availability_end = datetime.combine(
            local_day,
            end_time,
            tzinfo=timezone.utc
        )

        slot_local = availability_start

        # Generate slots from the ORIGINAL start time
        while slot_local + timedelta(minutes=duration) <= availability_end:

            slot_end = slot_local + timedelta(minutes=duration)

            # Check whether this slot overlaps any blackout
            is_blackout = False

            for blackout in blackout_rows:

                blackout_start = blackout["start_datetime"].replace(
                    tzinfo=timezone.utc
                )

                blackout_end = blackout["end_datetime"].replace(
                    tzinfo=timezone.utc
                )

                # Overlap condition
                if (
                    slot_local < blackout_end
                    and slot_end > blackout_start
                ):
                    is_blackout = True
                    break

            if not is_blackout:

                start_iso = utc_iso(slot_local)
                end_iso = utc_iso(slot_end)

                slot_id = build_slot_id(
                    doctor["doctor_id"],
                    pattern["consultation_type_id"],
                    start_iso
                )

                try:
                    slots_table.put_item(
                        Item={
                            "slot_id": slot_id,
                            "doctor_id": doctor["doctor_id"],
                            "consultation_type_id": pattern[
                                "consultation_type_id"
                            ],
                            "consultation_name": pattern[
                                "consultation_name"
                            ],
                            "start_time": start_iso,
                            "end_time": end_iso,
                            "local_date": local_day.isoformat(),
                            "capacity": int(pattern["capacity"]),
                            "booked_count": 0,
                            "version": 0,
                            "status": 1
                        },
                        ConditionExpression="attribute_not_exists(slot_id)"
                    )

                    created += 1

                except ClientError as error:

                    if error.response["Error"]["Code"] != (
                        "ConditionalCheckFailedException"
                    ):
                        raise

            # Always move forward using the original duration
            slot_local = slot_end

    return created


def materialize_handler(event, context):
    started_at = perf_counter()
    request_id = getattr(context, "aws_request_id", "unknown")
    connection = None
    created_count = 0
    processed_dates = 0
    try:
        window_days = int(
            get_parameter(f"/medislot/{ENVIRONMENT}/booking-window-days")
        )
        if not 1 <= window_days <= 90:
            raise ValueError("booking-window-days must be between 1 and 90")
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.doctor_id, ct.consultation_type_id,
                       ct.consultation_name, ct.duration_minutes, ct.capacity,
                       ap.day_of_week, ap.start_time, ap.end_time
                FROM doctors d
                JOIN doctor_consulting_types dct
                  ON dct.doctor_id = d.doctor_id AND dct.is_active = TRUE
                JOIN consultation_types ct
                  ON ct.consultation_type_id = dct.consultation_type_id
                JOIN availability_patterns ap
                  ON ap.consultation_type_id = ct.consultation_type_id
                 AND ap.doctor_id = d.doctor_id
                JOIN users u ON u.user_id = d.user_id AND u.is_active = TRUE
                ORDER BY d.doctor_id, ct.consultation_type_id
                """
            )
            pattern_rows = cursor.fetchall()
            cursor.execute("SELECT doctor_id FROM doctors")
            doctors = cursor.fetchall()
            cursor.execute(
                "SELECT doctor_id, start_datetime, end_datetime FROM blackout_dates"
            )
            blackout_rows = cursor.fetchall()

        patterns_by_doctor = {}
        for pattern in pattern_rows:
            patterns_by_doctor.setdefault(pattern["doctor_id"], []).append(pattern)
        blackouts_by_doctor = {}
        for blackout in blackout_rows:
            blackouts_by_doctor.setdefault(blackout["doctor_id"], []).append(blackout)

        requested_start_date = None
        requested_end_date = None
        if isinstance(event, dict):
            requested_start_date = event.get("start_date", event.get("date"))
            requested_end_date = event.get("end_date", requested_start_date)
        if requested_start_date:
            requested_start_date = date.fromisoformat(requested_start_date)
            requested_end_date = date.fromisoformat(requested_end_date)
            if requested_end_date < requested_start_date:
                raise ValueError("end_date must be on or after start_date")

        for doctor in doctors:
            start_day = (
                requested_start_date
                if requested_start_date
                else datetime.now(timezone.utc).date()
            )
            days = (
                (requested_end_date - requested_start_date).days + 1
                if requested_start_date
                else window_days
            )
            patterns = patterns_by_doctor.get(doctor["doctor_id"], [])
            blackouts = blackouts_by_doctor.get(doctor["doctor_id"], [])
            for offset in range(days):
                local_day = start_day + timedelta(days=offset)
                created_count += materialize_for_doctor_day(
                    doctor, patterns, blackouts, local_day
                )
                processed_dates += 1

        duration_ms = (perf_counter() - started_at) * 1000
        publish_metric("SlotMaterializationRuns", 1)
        publish_metric("SlotMaterializationCreatedSlots", created_count)
        publish_metric("SlotMaterializationDoctorDates", processed_dates)
        publish_metric("SlotMaterializationDurationMs", duration_ms, "Milliseconds")
        logger.info(
            "Slot materialization completed: doctor_dates=%s created_slots=%s duration_ms=%.2f request_id=%s",
            processed_dates,
            created_count,
            duration_ms,
            request_id,
        )
        return {
            "statusCode": 200,
            "created_slots": created_count,
            "processed_dates": processed_dates,
        }
    except Exception as error:
        duration_ms = (perf_counter() - started_at) * 1000
        publish_metric("SlotMaterializationErrors", 1)
        publish_metric("SlotMaterializationDurationMs", duration_ms, "Milliseconds")
        logger.exception(
            "Slot materialization failed: error_type=%s request_id=%s",
            type(error).__name__,
            request_id,
        )
        raise
    finally:
        if connection:
            connection.close()
