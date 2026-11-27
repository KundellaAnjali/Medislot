import logging
import os
import time
import uuid
from datetime import timedelta

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

try:
    from .milestone6_common import active_offer_lock_key, get_parameter, publish_event, publish_metric, slot_key, utc_now
except ImportError:
    from milestone6_common import active_offer_lock_key, get_parameter, publish_event, publish_metric, slot_key, utc_now


logger = logging.getLogger(__name__)
serializer = TypeSerializer()
client = boto3.client("dynamodb")
dynamodb = boto3.resource("dynamodb")
WAITLIST_TABLE = os.environ["WAITLIST_TABLE_NAME"]
OFFERS_TABLE = os.environ["WAITLIST_OFFERS_TABLE_NAME"]
SLOTS_TABLE = os.environ["SLOTS_TABLE_NAME"]
IDEMPOTENCY_TABLE = os.environ["IDEMPOTENCY_TABLE_NAME"]
waitlists = dynamodb.Table(WAITLIST_TABLE)
offers = dynamodb.Table(OFFERS_TABLE)
slots = dynamodb.Table(SLOTS_TABLE)


def av(value):
    return serializer.serialize(value)


def query_all(table, **parameters):
    items = []
    while True:
        page = table.query(**parameters)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        parameters["ExclusiveStartKey"] = last_key


def scan_all(table):
    items = []
    parameters = {}
    while True:
        page = table.scan(**parameters)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        parameters["ExclusiveStartKey"] = last_key


def promote_slot(detail):
    doctor_id = int(detail["doctor_id"])
    start_time = slot_key(detail["slot_start_time"])
    slot_id = int(detail["slot_id"])
    lock_key = {"doctor_id": doctor_id, "slot_start_time": start_time}
    slot = slots.get_item(Key=lock_key, ConsistentRead=True).get("Item")
    if not slot:
        raise RuntimeError("The freed appointment slot could not be found.")
    active_lock_key = active_offer_lock_key(doctor_id, start_time)
    active_lock = client.get_item(
        TableName=IDEMPOTENCY_TABLE,
        Key={"idempotency_key": {"S": active_lock_key}},
        ConsistentRead=True,
    ).get("Item")
    if active_lock:
        return publish_unconfirmed_offer(
            {"offer_id": int(active_lock["offer_id"]["N"])}
        )

    queue = query_all(
        waitlists,
        KeyConditionExpression=Key("slot_id").eq(slot_id),
        ScanIndexForward=True,
    )
    for entry in queue:
        if entry.get("status") != "WAITING":
            continue
        now = utc_now()
        offer_id = (time.time_ns() + uuid.uuid4().int) % (2**63 - 1) + 1
        duration_minutes = get_parameter("WAITLIST_OFFER_DURATION_MINUTES")
        expires_at = int((now + timedelta(minutes=duration_minutes)).timestamp())
        offer = {
            "offer_id": offer_id,
            "patient_id": int(entry["patient_id"]),
            "doctor_id": doctor_id,
            "slot_id": slot_id,
            "slot_start_time": start_time,
            "consultation_type_id": int(entry["consultation_type_id"]),
            "payment_method": entry.get("payment_method", "UPI"),
            "waitlist_id": entry["waitlist_id"],
            "joined_at": entry["joined_at"],
            "status": "ACTIVE",
            "expires_at": expires_at,
            "event_published": False,
        }
        try:
            now = utc_now()
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": IDEMPOTENCY_TABLE,
                            "Item": {
                                "idempotency_key": {"S": active_lock_key},
                                "offer_id": {"N": str(offer_id)},
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
                            "ConditionExpression": (
                                "attribute_not_exists(idempotency_key)"
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": SLOTS_TABLE,
                            "Key": {
                                "doctor_id": {"N": str(doctor_id)},
                                "slot_start_time": {"S": start_time},
                            },
                            "UpdateExpression": (
                                "SET booked_count = booked_count + :one, "
                                "version = version + :one"
                            ),
                            "ConditionExpression": (
                                "#status = :active AND booked_count = :booked "
                                "AND version = :version AND booked_count < #capacity "
                            ),
                            "ExpressionAttributeNames": {
                                "#status": "status",
                                "#capacity": "capacity",
                            },
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":active": {"N": "1"},
                                ":booked": {"N": str(int(slot.get("booked_count", 0)))},
                                ":version": {"N": str(int(slot["version"]))},
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": OFFERS_TABLE,
                            "Item": {name: av(value) for name, value in offer.items()},
                            "ConditionExpression": "attribute_not_exists(offer_id)",
                        }
                    },
                    {
                        "Update": {
                            "TableName": WAITLIST_TABLE,
                            "Key": {
                                "slot_id": {"N": str(slot_id)},
                                "joined_at": {"S": entry["joined_at"]},
                            },
                            "UpdateExpression": "SET #status = :offered",
                            "ConditionExpression": (
                                "waitlist_id = :waitlist_id AND #status = :waiting"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":offered": {"S": "OFFERED"},
                                ":waiting": {"S": "WAITING"},
                                ":waitlist_id": {"S": entry["waitlist_id"]},
                            },
                        }
                    },
                ]
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                current_lock = client.get_item(
                    TableName=IDEMPOTENCY_TABLE,
                    Key={"idempotency_key": {"S": active_lock_key}},
                    ConsistentRead=True,
                ).get("Item")
                if current_lock:
                    return publish_unconfirmed_offer(
                        {"offer_id": int(current_lock["offer_id"]["N"])}
                    )
                continue
            raise
        publish_metric("WaitlistPromotions")
        publish_unconfirmed_offer(
            {
                "doctor_id": doctor_id,
                "slot_start_time": start_time,
                "offer_id": offer_id,
                "expires_at": expires_at,
            }
        )
        return {"success": True, "offer_id": offer_id}
    return {"success": True, "offer_id": None}


def publish_unconfirmed_offer(lock):
    offer_id = int(lock["offer_id"])
    offer = offers.get_item(
        Key={"offer_id": offer_id}, ConsistentRead=True
    ).get("Item")
    if not offer:
        raise RuntimeError(f"Active waitlist offer {offer_id} was not found.")
    if offer.get("event_published"):
        return {"success": True, "offer_id": offer_id}
    publish_event(
        "WaitlistHoldOfferIssued",
        {
            "notification_id": f"waitlist-offer-issued-{offer_id}",
            "offer_id": str(offer_id),
            "patient_id": str(offer["patient_id"]),
            "doctor_id": str(offer["doctor_id"]),
            "slot_id": str(offer["slot_id"]),
            "slot_start_time": offer["slot_start_time"],
            "expires_at": offer["expires_at"],
            "status": "ACTIVE",
        },
    )
    offers.update_item(
        Key={"offer_id": offer_id},
        UpdateExpression="SET event_published = :published",
        ConditionExpression="status = :active",
        ExpressionAttributeValues={":published": True, ":active": "ACTIVE"},
    )
    return {"success": True, "offer_id": offer_id}


def expire_offer(offer):
    offer_id = int(offer["offer_id"])
    patient_id = int(offer["patient_id"])
    slot_id = int(offer["slot_id"])
    doctor_id = int(offer["doctor_id"])
    start_time = slot_key(offer["slot_start_time"])
    queue = query_all(
        waitlists,
        KeyConditionExpression=Key("slot_id").eq(slot_id),
        ScanIndexForward=True,
    )
    next_entry = next(
        (item for item in queue if item.get("status") == "WAITING"),
        None,
    )
    next_offer = None
    if next_entry:
        now = utc_now()
        next_offer_id = (time.time_ns() + uuid.uuid4().int) % (2**63 - 1) + 1
        expires_at = int(
            (
                now
                + timedelta(
                    minutes=get_parameter("WAITLIST_OFFER_DURATION_MINUTES")
                )
            ).timestamp()
        )
        next_offer = {
            "offer_id": next_offer_id,
            "patient_id": int(next_entry["patient_id"]),
            "doctor_id": doctor_id,
            "slot_id": slot_id,
            "slot_start_time": start_time,
            "consultation_type_id": int(next_entry["consultation_type_id"]),
            "payment_method": next_entry.get("payment_method", "UPI"),
            "waitlist_id": next_entry["waitlist_id"],
            "joined_at": next_entry["joined_at"],
            "status": "ACTIVE",
            "expires_at": expires_at,
            "event_published": False,
        }
    patient_membership_key = {
        "slot_id": {"N": str(slot_id)},
        "joined_at": {"S": f"MEMBERSHIP#{patient_id}"},
    }
    patient_membership = waitlists.get_item(
        Key={"slot_id": slot_id, "joined_at": f"MEMBERSHIP#{patient_id}"},
        ConsistentRead=True,
    ).get("Item")
    active_lock_key = active_offer_lock_key(doctor_id, start_time)
    transact_items = [
        {
            "Update": {
                "TableName": OFFERS_TABLE,
                "Key": {"offer_id": {"N": str(offer_id)}},
                "UpdateExpression": "SET #status = :expired",
                "ConditionExpression": "#status = :active AND expires_at <= :now",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                        ":expired": {"S": "EXPIRED"},
                        ":active": {"S": "ACTIVE"},
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
            "Update": {
                "TableName": SLOTS_TABLE,
                "Key": {
                        "doctor_id": {"N": str(doctor_id)},
                        "slot_start_time": {"S": start_time},
                },
                "UpdateExpression": (
                        "SET version = version + :one"
                        if next_offer
                        else "SET booked_count = booked_count - :one, "
                        "version = version + :one"
                ),
                "ConditionExpression": "booked_count >= :one",
                "ExpressionAttributeValues": {":one": {"N": "1"}},
            }
        },
    ]
    if patient_membership:
        transact_items.extend(
            [
                {
                        "Delete": {
                            "TableName": WAITLIST_TABLE,
                            "Key": patient_membership_key,
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
                            "UpdateExpression": (
                                "SET active_waitlists = active_waitlists - :one"
                            ),
                            "ConditionExpression": "active_waitlists >= :one",
                            "ExpressionAttributeValues": {":one": {"N": "1"}},
                        }
                },
            ]
        )
    if next_offer:
        now = utc_now()
        transact_items.extend(
            [
                {
                        "Update": {
                            "TableName": IDEMPOTENCY_TABLE,
                            "Key": {"idempotency_key": {"S": active_lock_key}},
                            "UpdateExpression": (
                                "SET offer_id = :new_offer, expires_at = :expires"
                            ),
                            "ConditionExpression": "offer_id = :old_offer",
                            "ExpressionAttributeValues": {
                                ":new_offer": {"N": str(next_offer["offer_id"])},
                                ":old_offer": {"N": str(offer_id)},
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
                            },
                        }
                },
                {
                        "Put": {
                            "TableName": OFFERS_TABLE,
                            "Item": {
                                name: av(value) for name, value in next_offer.items()
                            },
                            "ConditionExpression": "attribute_not_exists(offer_id)",
                        }
                },
                {
                        "Update": {
                            "TableName": WAITLIST_TABLE,
                            "Key": {
                                "slot_id": {"N": str(slot_id)},
                                "joined_at": {"S": next_entry["joined_at"]},
                            },
                            "UpdateExpression": "SET #status = :offered",
                            "ConditionExpression": (
                                "waitlist_id = :waitlist_id AND #status = :waiting"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": {
                                ":offered": {"S": "OFFERED"},
                                ":waiting": {"S": "WAITING"},
                                ":waitlist_id": {"S": next_entry["waitlist_id"]},
                            },
                        }
                },
            ]
        )
    else:
        transact_items.append(
            {
                "Delete": {
                        "TableName": IDEMPOTENCY_TABLE,
                        "Key": {"idempotency_key": {"S": active_lock_key}},
                        "ConditionExpression": "offer_id = :offer_id",
                        "ExpressionAttributeValues": {
                            ":offer_id": {"N": str(offer_id)}
                        },
                }
            }
        )
    try:
        client.transact_write_items(TransactItems=transact_items)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            latest = offers.get_item(
                Key={"offer_id": offer_id}, ConsistentRead=True
            ).get("Item")
            return bool(latest and latest.get("status") == "EXPIRED")
        raise
    publish_metric("OffersExpired")
    publish_event(
        "OfferExpired",
        {
            "notification_id": f"waitlist-offer-expired-{offer_id}",
            "offer_id": str(offer_id),
            "patient_id": str(patient_id),
            "doctor_id": str(doctor_id),
            "slot_id": str(slot_id),
            "slot_start_time": start_time,
            "status": "EXPIRED",
        },
    )
    if next_offer:
        publish_metric("WaitlistPromotions")
        publish_unconfirmed_offer(
            {
                "doctor_id": doctor_id,
                "slot_start_time": start_time,
                "offer_id": next_offer["offer_id"],
            }
        )
    return True


def expire_due_offers():
    now = int(utc_now().timestamp())
    due_offers = [
        offer
        for offer in scan_all(offers)
        if offer.get("status") == "ACTIVE"
        and int(offer.get("expires_at", 0)) <= now
    ]
    expired_count = sum(1 for offer in due_offers if expire_offer(offer))
    return expired_count
