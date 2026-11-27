import json
import logging
import os
from datetime import datetime, timezone

import boto3

try:
    from .appointment_repository import get_connection
except ImportError:
    from appointment_repository import get_connection


logger = logging.getLogger(__name__)
ENVIRONMENT = os.environ.get("ENVIRONMENT_NAME", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
ssm = boto3.client("ssm")
eventbridge = boto3.client("events")
cloudwatch = boto3.client("cloudwatch")
parameter_cache = {}


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def patient_actor(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    user_id = authorizer.get("userId")
    if authorizer.get("role") != "PATIENT" or not user_id:
        return None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def doctor_actor(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    user_id = authorizer.get("userId")
    if authorizer.get("role") != "DOCTOR" or not user_id:
        return None
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    return user_id if user_id > 0 else None


def parse_body(event):
    try:
        value = json.loads(event.get("body") or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Request body must be valid JSON.") from error
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value


def get_parameter(parameter_name, *, integer=True):
    if parameter_name not in parameter_cache:
        if not ENVIRONMENT:
            raise RuntimeError("Milestone 6 environment is not configured.")
        result = ssm.get_parameter(
            Name=f"/medislot/{ENVIRONMENT}/{parameter_name}"
        )
        parameter_cache[parameter_name] = result["Parameter"]["Value"]
    value = parameter_cache[parameter_name]
    return int(value) if integer else value


def publish_metric(name, value=1):
    cloudwatch.put_metric_data(
        Namespace="MediSlot/Patient",
        MetricData=[{"MetricName": name, "Value": value, "Unit": "Count"}],
    )


def publish_event(detail_type, detail):
    result = eventbridge.put_events(
        Entries=[
            {
                "EventBusName": EVENT_BUS_NAME,
                "Source": "medislot.appointments",
                "DetailType": detail_type,
                "Detail": json.dumps(detail, separators=(",", ":"), default=str),
            }
        ]
    )
    entries = result.get("Entries", [])
    if result.get("FailedEntryCount", 0) or len(entries) != 1 or not entries[0].get(
        "EventId"
    ):
        raise RuntimeError(f"Could not publish {detail_type} event.")
    return entries[0]["EventId"]


def patient_id_for_user(user_id, connection_factory=None):
    connection = (connection_factory or get_connection)()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT patient_id FROM patients WHERE user_id = %s",
                (user_id,),
            )
            patient = cursor.fetchone()
            return int(patient["patient_id"]) if patient else None
    finally:
        connection.close()


def slot_key(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    precision = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=precision).replace("+00:00", "Z")


def active_offer_lock_key(doctor_id, start_time):
    return f"waitlist-active-offer:{doctor_id}:{slot_key(start_time)}"


def utc_now():
    return datetime.now(timezone.utc)
