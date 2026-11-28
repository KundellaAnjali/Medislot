import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

try:
    from .milestone6_common import (
        get_parameter,
        parse_body,
        patient_actor,
        patient_id_for_user,
        get_connection,
        response,
        publish_event,
        slot_key,
        utc_now,
    )
except ImportError:
    from milestone6_common import (
        get_parameter,
        parse_body,
        patient_actor,
        patient_id_for_user,
        get_connection,
        response,
        publish_event,
        slot_key,
        utc_now,
    )


logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource("dynamodb")
client = boto3.client("dynamodb")
serializer = TypeSerializer()
WAITLIST_TABLE = os.environ["WAITLIST_TABLE_NAME"]
SLOTS_TABLE = os.environ["SLOTS_TABLE_NAME"]
waitlists = dynamodb.Table(WAITLIST_TABLE)
slots = dynamodb.Table(SLOTS_TABLE)


def av(value):
    return serializer.serialize(value)


def membership_key(patient_id, slot_id):
    return {
        "slot_id": slot_id,
        "joined_at": f"MEMBERSHIP#{patient_id}",
    }


def counter_key(patient_id):
    return {"slot_id": 0, "joined_at": f"PATIENT#{patient_id}"}


def query_patient_waitlists(patient_id):
    items = []
    parameters = {
        "IndexName": "PatientIdIndex",
        "KeyConditionExpression": Key("patient_id").eq(patient_id),
    }
    while True:
        page = waitlists.query(**parameters)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        parameters["ExclusiveStartKey"] = last_key


def lambda_handler(event, context):
    user_id = patient_actor(event)
    if user_id is None:
        return response(401, {"message": "Patient authentication is required."})
    try:
        patient_id = patient_id_for_user(user_id)
        if not patient_id:
            return response(404, {"message": "Patient profile was not found."})
        method = event.get("httpMethod", "").upper()
        if method == "POST":
            return response(201, join_waitlist(patient_id, parse_body(event)))
        if method == "GET":
            return response(200, list_waitlists(patient_id))
        if method == "DELETE":
            waitlist_id = (event.get("pathParameters") or {}).get("waitlist_id")
            return response(200, leave_waitlist(patient_id, waitlist_id))
        return response(405, {"message": "Method not allowed."})
    except ValueError as error:
        return response(400, {"message": str(error)})
    except WaitlistError as error:
        return response(error.status_code, {"message": str(error)})
    except Exception:
        logger.exception("Milestone 6 waitlist request failed")
        return response(500, {"message": "Could not process waitlist request."})


class WaitlistError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def join_waitlist(patient_id, body):
    required = {"doctor_id", "slot_id", "slot_start_time"}
    allowed = required | {"consultation_type_id", "payment_method"}
    if not required.issubset(body) or set(body) - allowed:
        raise ValueError(
            "Request must include doctor_id, slot_id, and slot_start_time."
        )
    try:
        doctor_id = int(body["doctor_id"])
        slot_id = int(body["slot_id"])
    except (TypeError, ValueError) as error:
        raise ValueError("doctor_id and slot_id must be positive integers.") from error
    if doctor_id < 1 or slot_id < 1:
        raise ValueError("doctor_id and slot_id must be positive integers.")
    start_time = slot_key(body["slot_start_time"])
    now = utc_now()
    parsed_start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    if parsed_start <= now:
        raise WaitlistError(409, "Past appointment slots cannot be waitlisted.")

    slot = slots.get_item(
        Key={"doctor_id": doctor_id, "slot_start_time": start_time},
        ConsistentRead=True,
    ).get("Item")
    if not slot or int(slot.get("slot_id", -1)) != slot_id:
        raise WaitlistError(404, "Appointment slot was not found.")
    if int(slot.get("status", 0)) != 1:
        raise WaitlistError(409, "This appointment slot is not active.")
    if not is_full(slot):
        raise WaitlistError(409, "Waitlists are available only for full slots.")
    if patient_has_active_booking(patient_id, doctor_id, slot_id):
        raise WaitlistError(
            409,
            "You already have an active appointment for this slot.",
        )

    waitlist_id = uuid.uuid4().hex
    joined_at = now.isoformat(timespec="microseconds") + "#" + waitlist_id
    consultation_type_id = int(
        body.get("consultation_type_id", slot["consultation_type_id"])
    )
    payment_method = str(body.get("payment_method", "UPI")).upper()
    if payment_method not in {
        "CREDIT_CARD",
        "DEBIT_CARD",
        "UPI",
        "NET_BANKING",
        "WALLET",
        "CASH",
    }:
        raise ValueError("payment_method is not supported.")
    waitlist_item = {
        "slot_id": slot_id,
        "joined_at": joined_at,
        "waitlist_id": waitlist_id,
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "slot_start_time": start_time,
        "consultation_type_id": consultation_type_id,
        "payment_method": payment_method,
        "status": "WAITING",
    }
    max_waitlists = get_parameter("MAX_ACTIVE_WAITLISTS")
    membership = membership_key(patient_id, slot_id)
    patient_counter = counter_key(patient_id)
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": WAITLIST_TABLE,
                        "Item": {name: av(value) for name, value in waitlist_item.items()},
                        "ConditionExpression": (
                            "attribute_not_exists(slot_id) "
                            "AND attribute_not_exists(joined_at)"
                        ),
                    }
                },
                {
                    "Put": {
                        "TableName": WAITLIST_TABLE,
                        "Item": {
                            "slot_id": {"N": str(slot_id)},
                            "joined_at": {"S": membership["joined_at"]},
                            "patient_id": {"N": str(patient_id)},
                            "waitlist_id": {"S": waitlist_id},
                            "status": {"S": "MEMBERSHIP"},
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(slot_id) "
                            "AND attribute_not_exists(joined_at)"
                        ),
                    }
                },
                {
                    "Update": {
                        "TableName": WAITLIST_TABLE,
                        "Key": {
                            "slot_id": {"N": "0"},
                            "joined_at": {"S": patient_counter["joined_at"]},
                        },
                        "UpdateExpression": (
                            "SET active_waitlists = "
                            "if_not_exists(active_waitlists, :zero) + :one, "
                            "patient_id = :patient, #status = :counter"
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(active_waitlists) "
                            "OR active_waitlists < :maximum"
                        ),
                        "ExpressionAttributeValues": {
                            ":zero": {"N": "0"},
                            ":one": {"N": "1"},
                            ":maximum": {"N": str(max_waitlists)},
                            ":patient": {"N": str(patient_id)},
                            ":counter": {"S": "COUNTER"},
                        },
                        "ExpressionAttributeNames": {"#status": "status"},
                    }
                },
                {
                    "ConditionCheck": {
                        "TableName": SLOTS_TABLE,
                        "Key": {
                            "doctor_id": {"N": str(doctor_id)},
                            "slot_start_time": {"S": start_time},
                        },
                        "ConditionExpression": (
                            "#status = :active AND booked_count >= #capacity"
                        ),
                        "ExpressionAttributeNames": {
                            "#status": "status",
                            "#capacity": "capacity",
                        },
                        "ExpressionAttributeValues": {
                            ":active": {"N": "1"},
                        },
                    }
                },
            ]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        existing = waitlists.get_item(
            Key={
                "slot_id": membership["slot_id"],
                "joined_at": membership["joined_at"],
            },
            ConsistentRead=True,
        ).get("Item")
        if existing:
            raise WaitlistError(409, "You are already waitlisted for this slot.") from error
        count = waitlists.get_item(
            Key={
                "slot_id": patient_counter["slot_id"],
                "joined_at": patient_counter["joined_at"],
            },
            ConsistentRead=True,
        ).get("Item", {})
        if int(count.get("active_waitlists", 0)) >= max_waitlists:
            raise WaitlistError(
                409, f"You can have at most {max_waitlists} active waitlists."
            ) from error
        raise WaitlistError(
            409, "The slot is no longer full or has changed. Refresh availability."
        ) from error
    publish_event(
        "WaitlistJoined",
        {
            "notification_id": f"waitlist-joined-{waitlist_id}",
            "waitlist_id": waitlist_id,
            "patient_id": str(patient_id),
            "doctor_id": str(doctor_id),
            "slot_id": str(slot_id),
            "slot_start_time": start_time,
            "status": "WAITING",
        },
    )
    return {
        "success": True,
        "waitlist_id": waitlist_id,
        "status": "WAITING",
        "joined_at": joined_at,
    }


def is_full(slot):
    return int(slot.get("booked_count", 0)) >= int(slot.get("capacity", 0))


def patient_has_active_booking(patient_id, doctor_id, slot_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM appointments
                WHERE patient_id = %s
                  AND doctor_id = %s
                  AND slot_id = %s
                  AND status = 'BOOKED'
                  AND appointment_status = 'CONFIRMED'
                LIMIT 1
                """,
                (patient_id, doctor_id, slot_id),
            )
            return cursor.fetchone() is not None
    finally:
        connection.close()


def list_waitlists(patient_id):
    entries = [
        item
        for item in query_patient_waitlists(patient_id)
        if item.get("status") in {"WAITING", "OFFERED"}
    ]
    return {"waitlists": entries}


def leave_waitlist(patient_id, waitlist_id):
    if not isinstance(waitlist_id, str) or not waitlist_id:
        raise ValueError("waitlist_id is required.")
    entry = next(
        (
            item for item in query_patient_waitlists(patient_id)
            if item.get("waitlist_id") == waitlist_id
            and item.get("status") == "WAITING"
        ),
        None,
    )
    if not entry:
        raise WaitlistError(404, "Waitlist entry was not found.")
    if entry.get("status") != "WAITING":
        raise WaitlistError(
            409, "A waitlist entry with an active offer cannot be removed."
        )
    slot_id = int(entry["slot_id"])
    joined_at = entry["joined_at"]
    membership = membership_key(patient_id, slot_id)
    patient_counter = counter_key(patient_id)
    membership_item = waitlists.get_item(
        Key=membership,
        ConsistentRead=True,
    ).get("Item")
    transaction = [
        {
            "Delete": {
                "TableName": WAITLIST_TABLE,
                "Key": {
                    "slot_id": {"N": str(slot_id)},
                    "joined_at": {"S": joined_at},
                },
                "ConditionExpression": (
                    "patient_id = :patient AND waitlist_id = :waitlist "
                    "AND #status = :waiting"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":patient": {"N": str(patient_id)},
                    ":waitlist": {"S": waitlist_id},
                    ":waiting": {"S": "WAITING"},
                },
            }
        }
    ]
    if membership_item:
        transaction.extend(
            [
                {
                    "Delete": {
                        "TableName": WAITLIST_TABLE,
                        "Key": {
                            "slot_id": {"N": str(slot_id)},
                            "joined_at": {"S": membership["joined_at"]},
                        },
                        "ConditionExpression": "waitlist_id = :waitlist",
                        "ExpressionAttributeValues": {
                            ":waitlist": {"S": waitlist_id}
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": WAITLIST_TABLE,
                        "Key": {
                            "slot_id": {"N": "0"},
                            "joined_at": {"S": patient_counter["joined_at"]},
                        },
                        "UpdateExpression": (
                            "SET active_waitlists = active_waitlists - :one"
                        ),
                        "ConditionExpression": "active_waitlists >= :one",
                        "ExpressionAttributeValues": {":one": {"N": "1"}},
                    }
                },
            ]
        )
    try:
        client.transact_write_items(TransactItems=transaction)
    except ClientError as error:
        logger.exception("Could not remove waitlist_id=%s", waitlist_id)
        raise WaitlistError(409, "The waitlist entry changed; refresh and retry.") from error
    publish_event(
        "WaitlistLeft",
        {
            "notification_id": f"waitlist-left-{waitlist_id}",
            "waitlist_id": waitlist_id,
            "patient_id": str(patient_id),
            "slot_id": str(slot_id),
            "status": "REMOVED",
        },
    )
    return {"success": True, "waitlist_id": waitlist_id, "status": "REMOVED"}
