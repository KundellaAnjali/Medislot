import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

try:
    from .appointment_repository import get_connection
    from .milestone6_common import (
        active_offer_lock_key,
        get_parameter,
        parse_body,
        patient_actor,
        patient_id_for_user,
        publish_event,
        publish_metric,
        response,
        slot_key,
        utc_now,
    )
except ImportError:
    from appointment_repository import get_connection
    from milestone6_common import (
        active_offer_lock_key,
        get_parameter,
        parse_body,
        patient_actor,
        patient_id_for_user,
        publish_event,
        publish_metric,
        response,
        slot_key,
        utc_now,
    )


logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource("dynamodb")
client = boto3.client("dynamodb")
serializer = TypeSerializer()
OFFERS_TABLE = os.environ["WAITLIST_OFFERS_TABLE_NAME"]
WAITLIST_TABLE = os.environ["WAITLIST_TABLE_NAME"]
SLOTS_TABLE = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE = os.environ["IDEMPOTENCY_TABLE_NAME"]
offers = dynamodb.Table(OFFERS_TABLE)
waitlists = dynamodb.Table(WAITLIST_TABLE)


class OfferError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def av(value):
    return serializer.serialize(value)


def lambda_handler(event, context):
    user_id = patient_actor(event)
    if user_id is None:
        return response(401, {"message": "Patient authentication is required."})
    try:
        patient_id = patient_id_for_user(user_id)
        if not patient_id:
            return response(404, {"message": "Patient profile was not found."})
        method = event.get("httpMethod", "").upper()
        if method == "GET":
            result = offers.query(
                IndexName="PatientIdIndex",
                KeyConditionExpression=Key("patient_id").eq(patient_id),
            )
            return response(200, {"offers": result.get("Items", [])})
        if method == "POST":
            path_id = (event.get("pathParameters") or {}).get("offer_id", "")
            if not re.fullmatch(r"[1-9][0-9]{0,18}", str(path_id)):
                return response(400, {"message": "Invalid offer_id."})
            parse_body(event)
            return response(200, accept_offer(patient_id, int(path_id)))
        return response(405, {"message": "Method not allowed."})
    except ValueError as error:
        return response(400, {"message": str(error)})
    except OfferError as error:
        return response(error.status_code, {"message": str(error)})
    except Exception:
        logger.exception("Milestone 6 waitlist offer request failed")
        return response(500, {"message": "Could not process waitlist offer."})


def accept_offer(patient_id, offer_id, *, connection_factory=None, now=None):
    now = now or utc_now()
    offer = offers.get_item(
        Key={"offer_id": offer_id}, ConsistentRead=True
    ).get("Item")
    if not offer or int(offer.get("patient_id", -1)) != patient_id:
        raise OfferError(404, "Waitlist offer was not found.")
    if offer.get("status") != "ACTIVE":
        raise OfferError(409, "Waitlist offer is no longer active.")
    if int(offer.get("expires_at", 0)) <= int(now.timestamp()):
        raise OfferError(409, "Waitlist offer has expired.")

    start_time = slot_key(offer["slot_start_time"])
    lock_key = active_offer_lock_key(doctor_id, start_time)
    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    connection = (connection_factory or get_connection)()
    appointment_id = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT patient_id FROM patients
                WHERE patient_id = %s
                FOR UPDATE
                """,
                (patient_id,),
            )
            if not cursor.fetchone():
                raise OfferError(404, "Patient profile was not found.")
            cursor.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM appointments
                WHERE patient_id = %s AND doctor_id = %s
                  AND slot_start_time > UTC_TIMESTAMP(6)
                  AND status = 'BOOKED'
                  AND appointment_status = 'CONFIRMED'
                """,
                (patient_id, int(offer["doctor_id"])),
            )
            max_active = get_parameter("patient-booking/max-active-bookings")
            if int(cursor.fetchone()["active_count"]) >= max_active:
                raise OfferError(
                    409,
                    "You already have the maximum number of upcoming appointments with this doctor.",
                )
            cursor.execute(
                """
                SELECT fee FROM doctor_consulting_types
                WHERE doctor_id = %s AND consultation_type_id = %s
                  AND is_active = TRUE
                """,
                (int(offer["doctor_id"]), int(offer["consultation_type_id"])),
            )
            consultation = cursor.fetchone()
            if not consultation:
                raise OfferError(409, "The consultation is no longer available.")
            appointment_time = start.astimezone(timezone.utc).replace(tzinfo=None)
            cursor.execute(
                """
                INSERT INTO appointments
                    (patient_id, doctor_id, slot_id, consultation_type_id,
                     appointment_status, appointment_datetime, slot_start_time,
                     status, created_at)
                VALUES (%s, %s, %s, %s, 'CONFIRMED', %s, %s, 'BOOKED',
                        UTC_TIMESTAMP(6))
                """,
                (
                    patient_id,
                    int(offer["doctor_id"]),
                    int(offer["slot_id"]),
                    int(offer["consultation_type_id"]),
                    appointment_time,
                    appointment_time,
                ),
            )
            appointment_id = int(cursor.lastrowid)
            transaction_id = f"PAY-{uuid.uuid4().hex.upper()}"
            cursor.execute(
                """
                INSERT INTO payments
                    (appointment_id, amount, transaction_id, payment_method,
                     status, created_at, paid_at)
                VALUES (%s, %s, %s, %s, 'SUCCESS', UTC_TIMESTAMP(6),
                        UTC_TIMESTAMP(6))
                """,
                (
                    appointment_id,
                    consultation["fee"],
                    transaction_id,
                    offer.get("payment_method", "UPI"),
                ),
            )
        commit_offer(offer, appointment_id, transaction_id)
        connection.commit()
    except OfferError:
        connection.rollback()
        raise
    except ClientError as error:
        connection.rollback()
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            raise OfferError(
                409, "The offer changed while it was being accepted."
            ) from error
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    event_detail = {
        "notification_id": f"appointment-booked-offer-{offer_id}",
        "appointment_id": str(appointment_id),
        "patient_id": str(patient_id),
        "doctor_id": str(offer["doctor_id"]),
        "slot_id": str(offer["slot_id"]),
        "slot_start_time": start_time,
        "status": "BOOKED",
        "source_offer_id": str(offer_id),
    }
    publish_event("AppointmentBooked", event_detail)
    publish_event(
        "WaitlistPromotionAccepted",
        {
            "notification_id": f"waitlist-promotion-accepted-{offer_id}",
            "offer_id": str(offer_id),
            "appointment_id": str(appointment_id),
            "patient_id": str(patient_id),
            "doctor_id": str(offer["doctor_id"]),
            "slot_id": str(offer["slot_id"]),
            "status": "ACCEPTED",
        },
    )
    publish_metric("WaitlistPromotions")
    return {
        "success": True,
        "appointment_id": appointment_id,
        "offer_id": offer_id,
        "status": "ACCEPTED",
    }


def commit_offer(offer, appointment_id, transaction_id):
    offer_id = int(offer["offer_id"])
    patient_id = int(offer["patient_id"])
    doctor_id = int(offer["doctor_id"])
    slot_id = int(offer["slot_id"])
    start_time = slot_key(offer["slot_start_time"])
    client.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": SLOTS_TABLE,
                    "Key": {
                        "doctor_id": {"N": str(doctor_id)},
                        "slot_start_time": {"S": start_time},
                    },
                    "UpdateExpression": (
                        "SET version = version + :one"
                    ),
                    "ConditionExpression": "booked_count >= :one",
                    "ExpressionAttributeValues": {
                        ":one": {"N": "1"},
                        ":offer_id": {"N": str(offer_id)},
                    },
                }
            },
            {
                "Delete": {
                    "TableName": IDEMPOTENCY_TABLE,
                    "Key": {"idempotency_key": {"S": lock_key}},
                    "ConditionExpression": "offer_id = :offer_id",
                    "ExpressionAttributeValues": {
                        ":offer_id": {"N": str(offer_id)}
                    },
                }
            },
            {
                "Update": {
                    "TableName": OFFERS_TABLE,
                    "Key": {"offer_id": {"N": str(offer_id)}},
                    "UpdateExpression": "SET #status = :accepted, appointment_id = :appointment_id",
                    "ConditionExpression": (
                        "#status = :active AND patient_id = :patient_id "
                        "AND expires_at > :now"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":accepted": {"S": "ACCEPTED"},
                        ":active": {"S": "ACTIVE"},
                        ":patient_id": {"N": str(patient_id)},
                        ":appointment_id": {"N": str(appointment_id)},
                        ":now": {"N": str(int(utc_now().timestamp()))},
                    },
                }
            },
            {
                "Delete": {
                    "TableName": WAITLIST_TABLE,
                    "Key": {
                        "slot_id": {"N": str(slot_id)},
                        "joined_at": {"S": offer["joined_at"]},
                    },
                    "ConditionExpression": "waitlist_id = :waitlist_id",
                    "ExpressionAttributeValues": {
                        ":waitlist_id": {"S": offer["waitlist_id"]}
                    },
                }
            },
            {
                "Delete": {
                    "TableName": WAITLIST_TABLE,
                    "Key": {
                        "slot_id": {"N": str(slot_id)},
                        "joined_at": {"S": f"MEMBERSHIP#{patient_id}"},
                    },
                    "ConditionExpression": "waitlist_id = :waitlist_id",
                    "ExpressionAttributeValues": {
                        ":waitlist_id": {"S": offer["waitlist_id"]}
                    },
                }
            },
            {
                "Update": {
                    "TableName": WAITLIST_TABLE,
                    "Key": {
                        "slot_id": {"N": "0"},
                        "joined_at": {"S": f"PATIENT#{patient_id}"},
                    },
                    "UpdateExpression": "SET active_waitlists = active_waitlists - :one",
                    "ConditionExpression": "active_waitlists >= :one",
                    "ExpressionAttributeValues": {":one": {"N": "1"}},
                }
            },
        ]
    )
