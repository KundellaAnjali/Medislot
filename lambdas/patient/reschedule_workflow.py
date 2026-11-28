import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

try:
    from .appointment_repository import get_connection
    from .milestone6_common import (
        get_parameter,
        publish_event,
        publish_metric,
        india_now,
        slot_key,
        utc_now,
    )
except ImportError:
    from appointment_repository import get_connection
    from milestone6_common import (
        get_parameter,
        publish_event,
        publish_metric,
        india_now,
        slot_key,
        utc_now,
    )


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
dynamodb = boto3.resource("dynamodb")
client = boto3.client("dynamodb")
SLOTS_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE_NAME = os.environ["IDEMPOTENCY_TABLE_NAME"]
slots = dynamodb.Table(SLOTS_TABLE_NAME)


class RescheduleError(Exception):
    pass


def validate_and_claim(event, context):
    user_id = int(event["user_id"])
    appointment_id = int(event["appointment_id"])
    target_start = slot_key(event["target_slot_start_time"])
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id,
                       a.consultation_type_id,
                       COALESCE(a.slot_start_time, a.appointment_datetime)
                           AS slot_start_time,
                       a.status, a.appointment_status
                FROM appointments a
                JOIN patients p ON p.patient_id = a.patient_id
                WHERE a.appointment_id = %s AND p.user_id = %s
                FOR UPDATE
                """,
                (appointment_id, user_id),
            )
            appointment = cursor.fetchone()
            if not appointment:
                raise RescheduleError("Appointment was not found.")
            if (
                appointment["status"] != "BOOKED"
                or appointment["appointment_status"] != "CONFIRMED"
            ):
                raise RescheduleError("Only confirmed appointments can be rescheduled.")
            old_start = as_india(appointment["slot_start_time"])
            now = india_now()
            cutoff_hours = get_parameter("RESCHEDULE_CUTOFF_HOURS")
            max_attempts = get_parameter("RESCHEDULE_MAX_ATTEMPTS")
            if old_start - now <= timedelta(hours=cutoff_hours):
                raise RescheduleError(
                    f"Appointments cannot be rescheduled within {cutoff_hours} hours of their start."
                )
            if target_start == slot_key(old_start):
                raise RescheduleError("Choose a different appointment slot.")

        target_slot = slots.get_item(
            Key={
                "doctor_id": int(appointment["doctor_id"]),
                "slot_start_time": target_start,
            },
            ConsistentRead=True,
        ).get("Item")
        if not target_slot or int(target_slot.get("status", 0)) != 1:
            raise RescheduleError("The target slot is not available.")
        if as_india(target_start) <= now:
            raise RescheduleError("Past appointment slots cannot be selected.")
        if int(target_slot.get("consultation_type_id", -1)) != int(
            appointment["consultation_type_id"]
        ):
            raise RescheduleError(
                "Choose a slot for the appointment's consultation type."
            )
        attempt = reserve_reschedule_attempt(appointment_id, max_attempts)
        claim_target_slot(int(appointment["doctor_id"]), target_start, target_slot)
        claim = {
            "user_id": user_id,
            "appointment_id": appointment_id,
            "patient_id": int(appointment["patient_id"]),
            "doctor_id": int(appointment["doctor_id"]),
            "consultation_type_id": int(appointment["consultation_type_id"]),
            "attempt": attempt,
            "original_slot_id": int(appointment["slot_id"]),
            "original_slot_start_time": slot_key(old_start),
            "target_slot_id": int(target_slot["slot_id"]),
            "target_slot_start_time": target_start,
        }
        return claim
    finally:
        connection.close()


def reserve_reschedule_attempt(appointment_id, maximum):
    now = utc_now()
    try:
        result = client.update_item(
            TableName=IDEMPOTENCY_TABLE_NAME,
            Key={
                "idempotency_key": {
                    "S": f"reschedule-attempts:{appointment_id}"
                }
            },
            UpdateExpression="SET expires_at = :expires ADD attempts :one",
            ConditionExpression=(
                "attribute_not_exists(attempts) OR attempts < :maximum"
            ),
            ExpressionAttributeValues={
                ":expires": {
                    "N": str(
                        int(
                            (
                                now
                                + timedelta(
                                    days=get_parameter(
                                        "RESCHEDULE_IDEMPOTENCY_TTL_DAYS"
                                    )
                                )
                            ).timestamp()
                        )
                    )
                },
                ":one": {"N": "1"},
                ":maximum": {"N": str(maximum)},
            },
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise RescheduleError(
                f"An appointment may be rescheduled at most {maximum} times."
            ) from error
        raise
    return int(result["Attributes"]["attempts"]["N"])


def claim_target_slot(doctor_id, start_time, slot):
    booked = int(slot.get("booked_count", 0))
    if booked >= int(slot.get("capacity", 0)):
        raise RescheduleError("The target slot is fully booked.")
    slots.update_item(
        Key={"doctor_id": doctor_id, "slot_start_time": start_time},
        UpdateExpression=(
            "SET booked_count = booked_count + :one, "
            "version = version + :one"
        ),
        ConditionExpression=(
            "#status = :active AND booked_count = :booked "
            "AND version = :version AND booked_count < #capacity "
            "AND slot_start_time > :now"
        ),
        ExpressionAttributeNames={"#status": "status", "#capacity": "capacity"},
        ExpressionAttributeValues={
            ":one": 1,
            ":active": 1,
            ":booked": booked,
            ":version": int(slot["version"]),
            ":now": slot_key(india_now()),
        },
    )


def update_appointment(event, context):
    claim = event["claim"]
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE appointments
                SET slot_id = %s, slot_start_time = %s, appointment_datetime = %s
                WHERE appointment_id = %s AND patient_id = %s
                  AND doctor_id = %s AND slot_id = %s
                  AND slot_start_time = %s AND status = 'BOOKED'
                  AND appointment_status = 'CONFIRMED'
                """,
                (
                    claim["target_slot_id"],
                    as_db_datetime(claim["target_slot_start_time"]),
                    as_db_datetime(claim["target_slot_start_time"]),
                    claim["appointment_id"],
                    claim["patient_id"],
                    claim["doctor_id"],
                    claim["original_slot_id"],
                    as_db_datetime(claim["original_slot_start_time"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RescheduleError("Appointment changed; retry the reschedule.")
        connection.commit()
        return {"appointment_updated": True}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_original(event, context):
    claim = event["claim"]
    release_slot(
        claim,
        slot_id=claim["original_slot_id"],
        start_time=claim["original_slot_start_time"],
        marker_kind="original-release",
    )
    return {"released": True}


def compensate(event, context):
    claim = event.get("claim") or event["claimState"]["claim"]
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT slot_id, slot_start_time, status, appointment_status
                FROM appointments
                WHERE appointment_id = %s AND patient_id = %s
                FOR UPDATE
                """,
                (claim["appointment_id"], claim["patient_id"]),
            )
            current = cursor.fetchone()
            if not current:
                raise RescheduleError(
                    "Appointment was not found during compensation."
                )
            current_start = slot_key(current["slot_start_time"])
            if (
                int(current["slot_id"]) == int(claim["target_slot_id"])
                and current_start == claim["target_slot_start_time"]
                and current["status"] == "BOOKED"
                and current["appointment_status"] == "CONFIRMED"
            ):
                cursor.execute(
                    """
                    UPDATE appointments
                    SET slot_id = %s, slot_start_time = %s,
                        appointment_datetime = %s
                    WHERE appointment_id = %s AND patient_id = %s
                      AND slot_id = %s AND slot_start_time = %s
                    """,
                    (
                        claim["original_slot_id"],
                        as_db_datetime(claim["original_slot_start_time"]),
                        as_db_datetime(claim["original_slot_start_time"]),
                        claim["appointment_id"],
                        claim["patient_id"],
                        claim["target_slot_id"],
                        as_db_datetime(claim["target_slot_start_time"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RescheduleError(
                        "Could not restore the appointment during compensation."
                    )
            elif not (
                int(current["slot_id"]) == int(claim["original_slot_id"])
                and current_start == claim["original_slot_start_time"]
            ):
                raise RescheduleError(
                    "Appointment changed before compensation could restore it."
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    release_slot(
        claim,
        slot_id=claim["target_slot_id"],
        start_time=claim["target_slot_start_time"],
        marker_kind="compensation",
    )
    publish_metric("ReschedulesCompensated")
    return {"compensated": True}


def release_slot(claim, *, slot_id, start_time, marker_kind):
    marker = (
        f"reschedule:{marker_kind}:{claim['appointment_id']}:"
        f"{claim['attempt']}:{uuid.uuid5(uuid.NAMESPACE_URL, start_time).hex[:12]}"
    )
    now = utc_now()
    start_time = slot_key(start_time)
    current_slot = slots.get_item(
        Key={
            "doctor_id": int(claim["doctor_id"]),
            "slot_start_time": start_time,
        },
        ConsistentRead=True,
    ).get("Item")
    if not current_slot:
        raise RescheduleError("Appointment slot was not found during release.")
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": IDEMPOTENCY_TABLE_NAME,
                        "Item": {
                            "idempotency_key": {"S": marker},
                            "created_at": {"S": now.isoformat()},
                            "expires_at": {
                                "N": str(
                                    int(
                                        (
                                            now
                                            + timedelta(
                                                days=get_parameter(
                                                    "RESCHEDULE_IDEMPOTENCY_TTL_DAYS"
                                                )
                                            )
                                        ).timestamp()
                                    )
                                )
                            },
                        },
                        "ConditionExpression": "attribute_not_exists(idempotency_key)",
                    }
                },
                {
                    "Update": {
                        "TableName": SLOTS_TABLE_NAME,
                        "Key": {
                            "doctor_id": {"N": str(claim["doctor_id"])},
                            "slot_start_time": {"S": slot_key(start_time)},
                        },
                        "UpdateExpression": (
                            "SET booked_count = booked_count - :one, "
                            "version = version + :one"
                        ),
                        "ConditionExpression": (
                            "booked_count >= :one"
                        ),
                        "ExpressionAttributeValues": {":one": {"N": "1"}},
                    }
                },
            ]
        )
    except ClientError as error:
        existing = client.get_item(
            TableName=IDEMPOTENCY_TABLE_NAME,
            Key={"idempotency_key": {"S": marker}},
            ConsistentRead=True,
        ).get("Item")
        if existing:
            return
        raise error


def publish_rescheduled(event, context):
    claim = event["claim"]
    publish_event(
        "AppointmentRescheduled",
        {
            "notification_id": (
                f"appointment-rescheduled-{claim['appointment_id']}-{claim['attempt']}"
            ),
            "appointment_id": str(claim["appointment_id"]),
            "patient_id": str(claim["patient_id"]),
            "doctor_id": str(claim["doctor_id"]),
            "old_slot_id": str(claim["original_slot_id"]),
            "old_slot_start_time": claim["original_slot_start_time"],
            "slot_id": str(claim["target_slot_id"]),
            "slot_start_time": claim["target_slot_start_time"],
            "status": "BOOKED",
        },
    )
    publish_event(
        "SlotSeatFreed",
        {
            "notification_id": (
                f"slot-seat-freed-reschedule-{claim['appointment_id']}-{claim['attempt']}"
            ),
            "appointment_id": str(claim["appointment_id"]),
            "doctor_id": str(claim["doctor_id"]),
            "slot_id": str(claim["original_slot_id"]),
            "slot_start_time": claim["original_slot_start_time"],
            "status": "SEAT_FREED",
        },
    )
    publish_metric("ReschedulesSucceeded")
    return {
        "success": True,
        "appointment_id": claim["appointment_id"],
        "status": "BOOKED",
        "slot_start_time": claim["target_slot_start_time"],
    }


def as_india(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+05:30"))
    return parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))


def as_db_datetime(value):
    return as_india(value).replace(tzinfo=None)
