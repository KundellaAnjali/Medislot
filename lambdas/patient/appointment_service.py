import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from .appointment_repository import AppointmentRepositoryError
except ImportError:
    from appointment_repository import AppointmentRepositoryError


IDEMPOTENCY_TTL_DAYS = 90
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
eventbridge = boto3.client("events")


class AppointmentServiceError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class AppointmentService:
    def __init__(
        self,
        repository,
        idempotency_table,
        get_slot,
        validate_slot,
        reserve_slot,
        release_slot,
    ):
        self.repository = repository
        self.idempotency_table = idempotency_table
        self.get_slot = get_slot
        self.validate_slot = validate_slot
        self.reserve_slot = reserve_slot
        self.release_slot = release_slot

    def book(self, user_id, key, doctor_id, slot_start_time):
        idempotency_key = hashlib.sha256(
            f"{user_id}:{key}".encode("utf-8")
        ).hexdigest()
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "doctor_id": doctor_id,
                    "slot_start_time": slot_start_time,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        try:
            cached = self._get_dynamodb_record(idempotency_key)
        except (BotoCoreError, ClientError) as error:
            raise AppointmentServiceError(
                503, "Idempotency storage is temporarily unavailable."
            ) from error
        if cached:
            self._validate_cached_request(cached, request_hash)
            self._publish_booking_event(cached, idempotency_key)
            return cached["response_payload"], False

        slot = self.get_slot(doctor_id, slot_start_time)
        if slot is None:
            raise AppointmentServiceError(404, "Appointment slot was not found.")
        self.validate_slot(slot)

        def reserve():
            return self.reserve_slot(doctor_id, slot_start_time, slot)

        def release(reserved_slot):
            self.release_slot(doctor_id, slot_start_time)

        try:
            payload, created = self.repository.create_booking(
                user_id=user_id,
                doctor_id=doctor_id,
                slot_start_time=slot_start_time,
                slot_id=int(slot["slot_id"]),
                consultation_type_id=int(slot["consultation_type_id"]),
                reserve_slot=reserve,
                release_slot=release,
            )
        except AppointmentRepositoryError as error:
            raise AppointmentServiceError(
                error.status_code, str(error)
            ) from error

        self._store_dynamodb_record(idempotency_key, request_hash, payload)
        self._publish_booking_event(
            {
                "request_hash": request_hash,
                "response_payload": payload,
                "event_published": False,
            },
            idempotency_key,
        )
        return payload, created

    def _get_dynamodb_record(self, idempotency_key):
        result = self.idempotency_table.get_item(
            Key={"idempotency_key": idempotency_key},
            ConsistentRead=True,
        )
        item = result.get("Item")
        if item and isinstance(item.get("response_payload"), str):
            item["response_payload"] = json.loads(item["response_payload"])
        return item

    def _store_dynamodb_record(self, idempotency_key, request_hash, payload):
        now = datetime.now(timezone.utc)
        try:
            self.idempotency_table.put_item(
                Item={
                    "idempotency_key": idempotency_key,
                    "appointment_id": int(payload["appointment_id"]),
                    "response_payload": json.dumps(
                        payload, separators=(",", ":")
                    ),
                    "request_hash": request_hash,
                    "event_published": False,
                    "created_at": now.isoformat(),
                    "ttl": int(
                        (now + timedelta(days=IDEMPOTENCY_TTL_DAYS)).timestamp()
                    ),
                },
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                try:
                    existing = self._get_dynamodb_record(idempotency_key)
                except ClientError as lookup_error:
                    raise AppointmentServiceError(
                        503,
                        "Could not verify the appointment idempotency record.",
                    ) from lookup_error
                if existing:
                    self._validate_cached_request(existing, request_hash)
                    return
            raise AppointmentServiceError(
                503, "Could not persist the appointment idempotency record."
            ) from error

    def _publish_booking_event(self, record, idempotency_key):
        if record.get("event_published"):
            return
        payload = record["response_payload"]
        event_detail = {
            "appointment_id": str(payload["appointment_id"]),
            "patient_id": str(payload["patient_id"]),
            "doctor_id": str(payload["doctor_id"]),
            "slot_id": str(payload["slot_id"]),
            "status": "BOOKED",
        }
        try:
            result = eventbridge.put_events(
                Entries=[
                    {
                        "EventBusName": EVENT_BUS_NAME,
                        "Source": "medislot.appointments",
                        "DetailType": "AppointmentBooked",
                        "Detail": json.dumps(event_detail, separators=(",", ":")),
                    }
                ]
            )
        except (BotoCoreError, ClientError) as error:
            raise AppointmentServiceError(
                503, "Appointment was saved, but its event could not be published."
            ) from error
        entries = result.get("Entries", [])
        if (
            result.get("FailedEntryCount", 0)
            or len(entries) != 1
            or not entries[0].get("EventId")
        ):
            raise AppointmentServiceError(
                503, "Appointment was saved, but its event could not be published."
            )
        try:
            self.idempotency_table.update_item(
                Key={"idempotency_key": idempotency_key},
                UpdateExpression="SET event_published = :published",
                ConditionExpression="request_hash = :request_hash",
                ExpressionAttributeValues={
                    ":published": True,
                    ":request_hash": record["request_hash"],
                },
            )
        except (BotoCoreError, ClientError) as error:
            raise AppointmentServiceError(
                503, "Appointment was saved, but its event status could not be recorded."
            ) from error

    @staticmethod
    def _validate_cached_request(record, request_hash):
        if record.get("request_hash") != request_hash:
            raise AppointmentServiceError(
                409,
                "Idempotency-Key was already used for a different request.",
            )
        if not record.get("response_payload"):
            raise AppointmentServiceError(
                503, "The original appointment request is still processing."
            )
