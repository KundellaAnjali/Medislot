import json
import logging
import os
import re

try:
    from .appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )
    from .appointment_cancellation import (
        AppointmentCancellationService,
        CancellationError,
    )
except ImportError:
    from appointment_repository import (
        AppointmentRepository,
        AppointmentRepositoryError,
    )
    from appointment_cancellation import AppointmentCancellationService, CancellationError


logger = logging.getLogger()
logger.setLevel(logging.INFO)
MAX_PAGE_SIZE = 100
MAX_OFFSET = 100000


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def parse_pagination(event):
    params = event.get("queryStringParameters") or {}
    try:
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
    except (TypeError, ValueError) as error:
        raise AppointmentRepositoryError(
            400, "limit and offset must be integers."
        ) from error
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise AppointmentRepositoryError(
            400, f"limit must be between 1 and {MAX_PAGE_SIZE}."
        )
    if not 0 <= offset <= MAX_OFFSET:
        raise AppointmentRepositoryError(
            400, f"offset must be between 0 and {MAX_OFFSET}."
        )
    return limit, offset


def lambda_handler(event, context):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    role = authorizer.get("role")
    user_id = authorizer.get("userId")
    if not user_id:
        return response(401, {"message": "Authentication is required."})
    if role not in {"PATIENT", "DOCTOR", "ADMIN"}:
        return response(403, {"message": "Role is not allowed."})
    try:
        user_id = int(user_id)
        if user_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return response(401, {"message": "Authentication is required."})

    method = event.get("httpMethod", "").upper()
    resource = event.get("resource", "")
    repository = AppointmentRepository()
    try:
        if method == "POST" and resource == "/appointments/{appointment_id}/cancel":
            raw_id = (event.get("pathParameters") or {}).get(
                "appointment_id", ""
            )
            if not re.fullmatch(r"[1-9][0-9]{0,9}", str(raw_id)):
                return response(400, {"message": "Invalid appointment_id."})
            result = AppointmentCancellationService().cancel(
                role,
                user_id,
                int(raw_id),
            )
            return response(200, result)
        if method == "POST" and resource == "/appointments/{appointment_id}/no-show":
            if role != "DOCTOR":
                return response(
                    403, {"message": "Only the assigned doctor can mark a no-show."}
                )
            raw_id = (event.get("pathParameters") or {}).get(
                "appointment_id", ""
            )
            if not re.fullmatch(r"[1-9][0-9]{0,9}", str(raw_id)):
                return response(400, {"message": "Invalid appointment_id."})
            result = AppointmentCancellationService().mark_no_show(
                role,
                user_id,
                int(raw_id),
            )
            return response(200, result)
        if method == "GET" and resource == "/patients/refunds":
            if role != "PATIENT":
                return response(
                    403,
                    {"message": "Only patients can view their refunds."},
                )
            limit, offset = parse_pagination(event)
            return response(
                200,
                repository.list_patient_refunds(user_id, limit, offset),
            )
        if method == "GET" and resource == "/appointments":
            limit, offset = parse_pagination(event)
            return response(
                200,
                repository.list_appointments(role, user_id, limit, offset),
            )
        if method == "GET" and resource == "/appointments/{appointment_id}":
            raw_id = (event.get("pathParameters") or {}).get(
                "appointment_id", ""
            )
            if not re.fullmatch(r"[1-9][0-9]{0,9}", str(raw_id)):
                return response(400, {"message": "Invalid appointment_id."})
            appointment = repository.get_appointment(
                role, user_id, int(raw_id)
            )
            if not appointment:
                return response(404, {"message": "Appointment was not found."})
            return response(200, {"appointment": appointment})
        return response(404, {"message": "Route not found."})
    except AppointmentRepositoryError as error:
        return response(error.status_code, {"message": str(error)})
    except CancellationError as error:
        return response(error.status_code, {"message": str(error)})
    except Exception:
        logger.exception("Appointment query failed: role=%s", role)
        return response(500, {"message": "Could not retrieve appointments."})
