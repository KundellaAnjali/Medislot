import json
import logging
import os
import re
from datetime import date

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
            raw_body = event.get("body")
            try:
                body = json.loads(raw_body) if raw_body else {}
            except (TypeError, json.JSONDecodeError):
                return response(400, {"message": "Request body must be valid JSON."})
            if not isinstance(body, dict) or set(body) - {"single_appointment"}:
                return response(400, {"message": "Invalid cancellation request."})
            single_appointment = body.get("single_appointment", False)
            if not isinstance(single_appointment, bool):
                return response(
                    400, {"message": "single_appointment must be a boolean."}
                )
            if single_appointment and role != "DOCTOR":
                return response(
                    403,
                    {"message": "Only doctors can cancel an individual appointment."},
                )
            result = AppointmentCancellationService().cancel(
                role,
                user_id,
                int(raw_id),
                single_appointment=single_appointment,
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
            return response(
                200,
                {"success": True, "status": result["status"]},
            )
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
            params = event.get("queryStringParameters") or {}
            appointment_date = params.get("appointment_date")
            appointment_status = params.get("appointment_status")
            search = params.get("search", "").strip()
            if appointment_date:
                try:
                    parsed_date = date.fromisoformat(appointment_date)
                except (TypeError, ValueError) as error:
                    raise AppointmentRepositoryError(
                        400, "appointment_date must use YYYY-MM-DD format."
                    ) from error
                if parsed_date.isoformat() != appointment_date:
                    raise AppointmentRepositoryError(
                        400, "appointment_date must use YYYY-MM-DD format."
                    )
            if appointment_status not in {
                None,
                "all",
                "booked",
                "cancelled",
                "completed",
                "no_show",
            }:
                raise AppointmentRepositoryError(
                    400,
                    "appointment_status must be all, booked, cancelled, completed, or no_show.",
                )
            if len(search) > 100:
                raise AppointmentRepositoryError(
                    400, "search must not exceed 100 characters."
                )
            return response(
                200,
                repository.list_appointments(
                    role,
                    user_id,
                    limit,
                    offset,
                    appointment_date=appointment_date if role == "DOCTOR" else None,
                    appointment_status=(
                        appointment_status if role == "DOCTOR" else None
                    ),
                    search=search if role == "DOCTOR" else None,
                ),
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
