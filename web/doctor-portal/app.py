import json
import logging
import os
import uuid
from time import perf_counter
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urljoin

import requests
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session

CONSULTATION_TYPES = (
    "Quick Consult",
    "Standard Consult",
    "Detailed Consult",
    "Health Camp Session",
)


app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app.config.update(
    SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR="/tmp/flask_session",
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
Session(app)

API_BASE_URL = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/") + "/"


def format_display_date(value):
    if isinstance(value, datetime):
        parsed_date = value.date()
    elif isinstance(value, date):
        parsed_date = value
    else:
        parsed_date = date.fromisoformat(str(value)[:10])
    return parsed_date.strftime("%d %b %Y")


app.jinja_env.filters["display_date"] = format_display_date


@app.before_request
def start_request_timer():
    g.request_started_at = perf_counter()


@app.after_request
def log_request(response):
    duration_ms = (perf_counter() - g.request_started_at) * 1000
    app.logger.info(
        "Portal request completed: method=%s path=%s status=%s duration_ms=%.2f",
        request.method,
        request.path,
        response.status_code,
        duration_ms,
    )
    return response


class ApiError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def role_required(role, profile_path, missing_profile_message):
    def decorate(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            access_token = session.get("access_token")
            refresh_token = session.get("refresh_token")
            if (
                not access_token
                or not refresh_token
                or session.get("role") != role
            ):
                return redirect(url_for("auth", mode="login", role=role))

            try:
                g.authorized_profile_response = api_request(
                    "GET",
                    profile_path,
                )
            except ApiError as error:
                if error.status_code == 401:
                    session.clear()
                    flash(
                        "Your session is invalid or has expired. Please sign in again.",
                        "error",
                    )
                    return redirect(url_for("auth", mode="login", role=role))
                if error.status_code == 403:
                    app.logger.warning(
                        "Portal session was denied after refresh: role=%s",
                        role,
                    )
                    return "You are not authorized to access this page.", 403
                if (
                    error.status_code == 404
                    and str(error) == missing_profile_message
                ):
                    g.authorized_profile_response = None
                else:
                    if error.status_code == 404:
                        session.clear()
                        flash(
                            "Your session could not be verified. Please sign in again.",
                            "error",
                        )
                        return redirect(
                            url_for("auth", mode="login", role=role)
                        )
                    app.logger.error(
                        "Could not verify session: role=%s status=%s",
                        role,
                        error.status_code,
                    )
                    return (
                        "Unable to verify your session. Please try again.",
                        503,
                    )

            return handler(*args, **kwargs)

        return wrapped

    return decorate


doctor_required = role_required(
    "DOCTOR",
    "/doctors/me",
    "Doctor profile not found",
)
patient_required = role_required(
    "PATIENT",
    "/patients/me",
    "Patient profile not found.",
)

def refresh_access_token():

    refresh_token = session.get("refresh_token")

    if not refresh_token:
        app.logger.warning("Access token refresh requested without a refresh token")
        raise ApiError("Your session has expired. Please sign in again.", 401)

    try:
        result = requests.post(
            urljoin(API_BASE_URL, "auth/refresh"),
            json={
                "refresh_token": refresh_token
            },
            timeout=15,
        )
    except requests.RequestException:
        app.logger.exception(
            "Failed to refresh access token"
        )
        raise ApiError("Session refresh is temporarily unavailable. Please try again.", 503)

    if result.status_code in {401, 403}:
        app.logger.warning(
            "Refresh token was rejected: status=%s",
            result.status_code,
        )
        session.clear()
        raise ApiError("Your session has expired. Please sign in again.", 401)
    if not result.ok:
        app.logger.error(
            "Refresh endpoint failed: status=%s",
            result.status_code,
        )
        raise ApiError("Session refresh is temporarily unavailable. Please try again.", 503)

    try:
        payload = result.json()
    except ValueError as error:
        app.logger.error("Refresh endpoint returned invalid JSON")
        raise ApiError("Session refresh returned an invalid response.", 503) from error

    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        app.logger.error("Refresh endpoint response did not contain an access token")
        raise ApiError("Session refresh returned an invalid response.", 503)

    session["access_token"] = access_token
    session.modified = True
    app.logger.info("Access token refreshed successfully")
    return True

def api_request(
    method,
    path,
    *,
    json_body=None,
    params=None,
    request_headers=None,
    authenticated=True
):
    headers = dict(request_headers or {})

    if authenticated:
        headers["Authorization"] = (
            f"Bearer {session['access_token']}"
        )

    try:
        result = requests.request(
            method,
            urljoin(
                API_BASE_URL,
                path.lstrip("/")
            ),
            headers=headers,
            json=json_body,
            params=params,
            timeout=15,
        )
    except requests.RequestException as error:
        app.logger.exception(
            "MediSlot API request failed: method=%s path=%s",
            method,
            path,
        )
        raise ApiError(
            f"MediSlot API connection failed ({type(error).__name__}: {error})"
        ) from error

    #
    # Access token expired
    #
    if authenticated and result.status_code in {401, 403}:

        app.logger.info(
            "Received authentication failure: status=%s. Attempting token refresh.",
            result.status_code,
        )

        if session.get("refresh_token"):
            refresh_access_token()

            headers["Authorization"] = (
                f"Bearer {session['access_token']}"
            )

            try:
                result = requests.request(
                    method,
                    urljoin(
                        API_BASE_URL,
                        path.lstrip("/")
                    ),
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=15,
                )
            except requests.RequestException as error:
                app.logger.exception(
                    "Retry request failed after token refresh"
                )
                raise ApiError(
                    f"MediSlot API connection failed "
                    f"({type(error).__name__}: {error})"
                ) from error

    try:
        payload = result.json()
    except ValueError:
        payload = {}

    if not result.ok:
        request_id = (
            result.headers.get("x-amzn-requestid")
            or result.headers.get("x-amz-apigw-id")
            or "not provided"
        )
        message = None
        if isinstance(payload, dict):
            message = (
                payload.get("message")
                or payload.get("Message")
                or payload.get("error")
                or payload.get("errorMessage")
            )
        if not message:
            content_type = result.headers.get("content-type", "unknown")
            response_text = result.text.strip()
            response_detail = ""
            if response_text and "html" not in content_type.lower():
                response_detail = f", response={response_text[:300]}"
            message = (
                f"API returned HTTP {result.status_code} without an error message "
                f"(content-type={content_type}, request_id={request_id}"
                f"{response_detail})"
            )
        app.logger.warning(
            "MediSlot API returned an error: "
            "method=%s path=%s status=%s request_id=%s message=%s",
            method,
            path,
            result.status_code,
            request_id,
            message,
        )

        raise ApiError(
            message,
            result.status_code,
        )

    app.logger.info(
        "MediSlot API request completed: "
        "method=%s path=%s status=%s",
        method,
        path,
        result.status_code,
    )

    return payload

def with_api_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)

        except ApiError as error:
            app.logger.warning(
                "Portal action failed: endpoint=%s status=%s",
                request.endpoint,
                error.status_code,
            )

            if error.status_code == 401:
                role = session.get("role")
                session.clear()
                flash(
                    "Your session expired. Please sign in again.",
                    "error"
                )
                return redirect(
                    url_for(
                        "auth",
                        mode="login",
                        role=role if role in {"DOCTOR", "PATIENT"} else None,
                    )
                )

            app.logger.error(
                "API FAILURE endpoint=%s status=%s error=%s",
                request.endpoint,
                error.status_code,
                str(error)
            )

            flash(str(error), "error")

            return redirect(
                request.referrer
                or url_for("doctor_dashboard")
            )

    return wrapped



@app.get("/")
def home():
    return redirect(url_for("auth", mode="login"))

@app.route("/auth", methods=["GET", "POST"])
def auth():
    mode = request.values.get("mode", "login")
    role = request.values.get("role", "DOCTOR").upper()
    if mode not in {"login", "register"}:
        mode = "login"
    if role not in {"DOCTOR", "PATIENT"}:
        role = "DOCTOR"
    if request.method == "GET":
        return render_template("auth.html", mode=mode, role=role)

    mode = request.form.get("mode", "login")
    role = request.form.get("role", "DOCTOR").upper()
    if mode not in {"login", "register"} or role not in {"DOCTOR", "PATIENT"}:
        flash("Choose a valid account type and action.", "error")
        return render_template("auth.html", mode="login", role="DOCTOR"), 400
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Enter your email and password.", "error")
        return render_template("auth.html", mode=mode, role=role), 400
    if mode == "register":
        payload = {
            "role": role,
            "first_name": request.form.get("first_name", "").strip(),
            "last_name": request.form.get("last_name", "").strip(),
            "email": email,
            "password": password,
        }
        if not payload["first_name"] or not payload["last_name"]:
            flash("Enter your first and last name.", "error")
            return render_template("auth.html", mode=mode, role=role), 400
        if password != request.form.get("confirm_password", ""):
            flash("Passwords do not match.", "error")
            return render_template("auth.html", mode=mode, role=role), 400
        try:
            api_request("POST", "/auth/register", json_body=payload, authenticated=False)
        except ApiError as error:
            flash(str(error), "error")
            return render_template("auth.html", mode=mode, role=role), error.status_code
        flash("Account created. Sign in to continue.", "success")
        return redirect(url_for("auth", mode="login", role=role))

    try:
        result = api_request(
            "POST",
            "/auth/login",
            json_body={"email": email, "password": password},
            authenticated=False,
        )
    except ApiError as error:
        flash(str(error), "error")
        return render_template("auth.html", mode=mode, role=role), error.status_code
    if result.get("role", "").upper() != role:
        flash("That account does not match the selected role.", "error")
        return render_template("auth.html", mode=mode, role=role), 403
    session.clear()
    session["access_token"] = result["access_token"]
    session["refresh_token"] = result["refresh_token"]
    session["role"] = role
    endpoint = "doctor_dashboard" if role == "DOCTOR" else "patient_dashboard"
    return redirect(url_for(endpoint))


@app.get("/doctor/login")
@app.get("/doctor/register")
def doctor_auth_alias():
    mode = "register" if request.path.endswith("/register") else "login"
    return redirect(url_for("auth", mode=mode, role="DOCTOR"))


@app.get("/patient/login")
@app.get("/patient/register")
def patient_auth_alias():
    mode = "register" if request.path.endswith("/register") else "login"
    return redirect(url_for("auth", mode=mode, role="PATIENT"))


@app.post("/doctor/logout")
@app.post("/logout")
def logout():
    if not session.get("refresh_token"):
        session.clear()
        return redirect(url_for("auth"))
    try:
        api_request(
            "POST",
            "/auth/logout",
            json_body={"refresh_token": session.get("refresh_token")},
            authenticated=False,
        )
    except ApiError as error:
        app.logger.warning("Logout request failed: status=%s", error.status_code)
    session.clear()
    return redirect(url_for("auth"))


@app.get("/dashboard/doctor")
@doctor_required
@with_api_errors
def doctor_dashboard():
    profile = g.authorized_profile_response
    consultations = api_request("GET", "/doctors/me/consultation-types").get(
        "consultation_types", []
    )
    availability = api_request("GET", "/doctors/me/availability").get("patterns", [])
    blackouts = api_request("GET", "/doctors/me/blackouts").get("blackouts", [])
    active_names = {
        item["consultation_name"]
        for item in consultations
        if item.get("is_active")
    }
    try:
        schedule_date = request.args.get("date") or datetime.now(timezone.utc).date().isoformat()
        schedule = api_request(
            "GET", "/doctors/me/schedule", params={"date": schedule_date}
        )
    except ApiError as error:
        if error.status_code == 404:
            schedule = {"slots": [], "date": schedule_date}
        else:
            raise
    schedule_filter = request.args.get("status", "all")
    if schedule_filter not in {"all", "booked", "blocked", "open"}:
        schedule_filter = "all"
    return render_template(
        "dashboard.html",
        profile=profile,
        consultations=consultations,
        available_consultation_types=[
            item for item in CONSULTATION_TYPES if item not in active_names
        ],
        patterns=availability,
        blackouts=blackouts,
        schedule=schedule,
        schedule_display_date=format_display_date(schedule["date"]),
        schedule_filter=schedule_filter,
    )


@app.get("/doctor")
def old_doctor_dashboard():
    return redirect(url_for("doctor_dashboard"))


@app.get("/dashboard/patient")
@patient_required
def patient_dashboard():
    profile_response = g.authorized_profile_response
    profile = profile_response.get("profile") if profile_response else None
    appointments_offset = max(
        request.args.get("appointments_offset", 0, type=int) or 0,
        0,
    )
    appointments, appointments_error, has_more, next_offset = get_patient_appointments(
        appointments_offset
    )
    if appointments_error and appointments_error.status_code in {401, 403}:
        return patient_api_error(appointments_error)
    if appointments_error:
        flash(f"Appointments could not be loaded: {appointments_error}", "error")
    return render_template(
        "patient_dashboard.html",
        profile=profile,
        appointments=appointments,
        appointments_error=appointments_error,
        appointments_offset=appointments_offset,
        appointments_has_more=has_more,
        appointments_next_offset=next_offset,
        today_display_date=format_display_date(datetime.now(timezone.utc).date()),
    )


def get_patient_appointments(offset=0):
    try:
        result = api_request(
            "GET",
            "/appointments",
            params={"limit": 50, "offset": offset},
        )
    except ApiError as error:
        return [], error, False, None
    if not isinstance(result, dict) or not isinstance(
        result.get("appointments"), list
    ):
        return (
            [],
            ApiError("Appointments API returned an invalid response."),
            False,
            None,
        )
    return (
        result["appointments"],
        None,
        bool(result.get("has_more")),
        result.get("next_offset"),
    )


def patient_api_error(error):
    if error.status_code == 401:
        session.clear()
        flash("Invalid credentials or your session has expired. Please sign in again.", "error")
        return redirect(url_for("auth", mode="login", role="PATIENT"))
    if error.status_code == 403:
        flash("You are not allowed to access this patient profile.", "error")
        return render_template(
            "patient_dashboard.html",
            profile=None,
            appointments=[],
            appointments_error=error,
            appointments_offset=0,
            appointments_has_more=False,
            appointments_next_offset=None,
            today_display_date=format_display_date(datetime.now(timezone.utc).date()),
        ), 403
    else:
        flash(str(error), "error")
    return redirect(url_for("patient_dashboard"))


@app.get("/patient/doctors")
@patient_required
def patient_doctor_search():
    filters = {
        field: request.args.get(field, "").strip()
        for field in ("name", "hospital", "specialization", "city")
    }
    try:
        page = request.args.get("page", "1")
        limit = request.args.get("limit", "10")
        results = api_request(
            "GET",
            "/doctors/search",
            params={**filters, "page": page, "limit": limit},
        )
    except ApiError as error:
        if error.status_code in {401, 403}:
            return patient_api_error(error)
        flash(str(error), "error")
        results = {
            "doctors": [],
            "pagination": {"page": 1, "limit": 10, "total": 0, "total_pages": 0},
        }

    profile_response = g.authorized_profile_response
    profile = profile_response.get("profile") if profile_response else None
    appointments, appointments_error, has_more, next_offset = get_patient_appointments()
    if appointments_error and appointments_error.status_code in {401, 403}:
        return patient_api_error(appointments_error)
    if appointments_error:
        flash(f"Appointments could not be loaded: {appointments_error}", "error")
    return render_template(
        "patient_dashboard.html",
        profile=profile,
        appointments=appointments,
        appointments_error=appointments_error,
        appointments_offset=0,
        appointments_has_more=has_more,
        appointments_next_offset=next_offset,
        doctor_search=results,
        doctor_search_filters=filters,
        today_display_date=format_display_date(datetime.now(timezone.utc).date()),
    )


@app.get("/patient/doctors/<int:doctor_id>/availability")
@patient_required
def patient_doctor_availability(doctor_id):
    profile_response = g.authorized_profile_response
    profile = profile_response.get("profile") if profile_response else None
    start_date = request.args.get("start_date") or datetime.now(
        timezone.utc
    ).date().isoformat()
    end_date = request.args.get("end_date")
    if not end_date:
        try:
            end_date = (
                datetime.fromisoformat(start_date).date() + timedelta(days=7)
            ).isoformat()
        except ValueError:
            end_date = start_date
    availability = None
    error_message = None
    status_code = 200
    consultation_type = request.args.get("consultation_type", "").strip()
    consultation_types = []

    try:
        public_profile_response = api_request(
            "GET",
            f"/doctors/{doctor_id}/profile",
            authenticated=False,
        )
        if not isinstance(public_profile_response, dict) or not isinstance(
            public_profile_response.get("profile"), dict
        ):
            raise ApiError("Doctor profile API returned an invalid response.")
        public_profile = public_profile_response["profile"]
        consultation_types = public_profile.get("consultation_details", [])
        if not isinstance(consultation_types, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("consultation_type"), str)
            for item in consultation_types
        ):
            raise ApiError("Doctor consultation types API returned an invalid response.")
    except ApiError as error:
        app.logger.warning(
            "Could not load consultation types for availability filter: "
            "doctor_id=%s status=%s",
            doctor_id,
            error.status_code,
        )

    try:
        payload = api_request(
            "GET",
            f"/doctors/{doctor_id}/availability",
            params={"start_date": start_date, "end_date": end_date},
        )
        if not isinstance(payload, dict) or not isinstance(
            payload.get("slots"), list
        ):
            raise ApiError("Availability API returned an invalid response.")
        availability = payload["slots"]
        if not consultation_types:
            consultation_types = [
                {"consultation_type": name}
                for name in sorted(
                    {
                        slot.get("consultation_type", "")
                        for slot in availability
                        if slot.get("consultation_type")
                    }
                )
            ]
        if consultation_type:
            availability = [
                slot
                for slot in availability
                if slot.get("consultation_type") == consultation_type
            ]
    except ApiError as error:
        if error.status_code in {401, 403}:
            return patient_api_error(error)
        error_message = str(error)
        status_code = error.status_code if error.status_code >= 400 else 502

    stored_keys = session.setdefault("appointment_idempotency_keys", {})
    idempotency_keys = {}
    for slot in availability or []:
        key = f"{doctor_id}:{slot['start_time']}"
        if key not in stored_keys:
            stored_keys[key] = uuid.uuid4().hex
        idempotency_keys[slot["start_time"]] = stored_keys[key]
    session["appointment_idempotency_keys"] = stored_keys

    return render_template(
        "patient_availability.html",
        profile=profile,
        doctor_id=doctor_id,
        start_date=start_date,
        end_date=end_date,
        consultation_types=consultation_types,
        selected_consultation_type=consultation_type,
        availability=availability,
        idempotency_keys=idempotency_keys,
        error_message=error_message,
    ), status_code


@app.post("/patient/appointments/book")
@patient_required
def book_appointment():
    doctor_id = request.form.get("doctor_id", type=int)
    slot_start_time = request.form.get("slot_start_time", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    consultation_type = request.form.get("consultation_type", "").strip()
    idempotency_key = request.form.get("idempotency_key", "").strip()
    if not doctor_id or doctor_id <= 0 or not slot_start_time:
        flash("Choose a valid appointment slot before booking.", "error")
        return redirect(url_for("patient_dashboard"))
    if not idempotency_key:
        flash("Booking request is missing its idempotency key. Please retry.", "error")
        return redirect(
            url_for(
                "patient_doctor_availability",
                doctor_id=doctor_id,
                **({"start_date": start_date} if start_date else {}),
                **({"end_date": end_date} if end_date else {}),
                **(
                    {"consultation_type": consultation_type}
                    if consultation_type
                    else {}
                ),
            )
        )


    try:
        result = api_request(
            "POST",
            "/appointments/book",
            json_body={
                "doctor_id": doctor_id,
                "slot_start_time": slot_start_time,
            },
            request_headers={"Idempotency-Key": idempotency_key},
        )
    except ApiError as error:
        if error.status_code in {401, 403}:
            return patient_api_error(error)
        flash(str(error), "error")
    else:
        if not isinstance(result, dict) or not result.get("appointment_id"):
            app.logger.error("Appointment booking API returned an invalid response.")
            flash(
                "Appointment booking response was invalid. "
                "Please check availability again.",
                "error",
            )
            return redirect(
                url_for(
                    "patient_doctor_availability",
                    doctor_id=doctor_id,
                    **({"start_date": start_date} if start_date else {}),
                    **({"end_date": end_date} if end_date else {}),
                    **(
                        {"consultation_type": consultation_type}
                        if consultation_type
                        else {}
                    ),
                )
            )
        appointment_id = result["appointment_id"]
        session_keys = session.get("appointment_idempotency_keys", {})
        session_keys.pop(f"{doctor_id}:{slot_start_time}", None)
        session["appointment_idempotency_keys"] = session_keys
        flash(
            f"Appointment booked successfully. Reference: {appointment_id}.",
            "success",
        )

    return redirect(
        url_for(
            "patient_doctor_availability",
            doctor_id=doctor_id,
            **({"start_date": start_date} if start_date else {}),
            **({"end_date": end_date} if end_date else {}),
            **(
                {"consultation_type": consultation_type}
                if consultation_type
                else {}
            ),
        )
    )


@app.post("/patient/appointments/<int:appointment_id>/cancel")
@patient_required
def cancel_patient_appointment(appointment_id):
    try:
        api_request("POST", f"/appointments/{appointment_id}/cancel")
    except ApiError as error:
        return patient_api_error(error)
    flash(f"Appointment {appointment_id} cancelled.", "success")
    return redirect(
        url_for("patient_dashboard", view="patient-appointments-view")
        + "#my-appointments"
    )


@app.post("/doctor/appointments/<int:appointment_id>/cancel")
@doctor_required
@with_api_errors
def cancel_doctor_appointment(appointment_id):
    api_request("POST", f"/appointments/{appointment_id}/cancel")
    flash(
        "The slot and all appointments booked for it have been cancelled.",
        "success",
    )
    return redirect_to_doctor_schedule()


def redirect_to_doctor_schedule():
    schedule_date = request.form.get("schedule_date", "").strip()
    status_filter = request.form.get("status_filter", "all")
    if status_filter not in {"all", "booked", "blocked", "open"}:
        status_filter = "all"
    return redirect(
        url_for(
            "doctor_dashboard",
            date=schedule_date,
            status=status_filter,
            view="schedule-view",
        )
        + "#schedule-view"
    )


@app.post("/doctor/appointments/bulk-cancel")
@doctor_required
@with_api_errors
def bulk_cancel_doctor_appointments():
    raw_ids = request.form.getlist("appointment_id")
    try:
        appointment_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    except (TypeError, ValueError):
        flash("The selected appointments could not be identified.", "error")
        return redirect_to_doctor_schedule()
    if not appointment_ids or len(appointment_ids) > 50 or any(
        appointment_id <= 0 for appointment_id in appointment_ids
    ):
        flash("Select between 1 and 50 valid booked slots.", "error")
        return redirect_to_doctor_schedule()

    cancelled = 0
    failures = []
    for appointment_id in appointment_ids:
        try:
            api_request("POST", f"/appointments/{appointment_id}/cancel")
        except ApiError as error:
            failures.append(appointment_id)
            app.logger.warning(
                "Bulk slot cancellation failed: appointment_id=%s status=%s",
                appointment_id,
                error.status_code,
            )
        else:
            cancelled += 1

    if failures:
        flash(
            f"{cancelled} slot(s) cancelled; {len(failures)} could not be cancelled.",
            "error" if not cancelled else "warning",
        )
    else:
        flash(f"{cancelled} slot(s) and their bookings cancelled.", "success")
    return redirect_to_doctor_schedule()


@app.get("/doctors/<int:doctor_id>")
def public_doctor_profile(doctor_id):
    try:
        payload = api_request(
            "GET",
            f"/doctors/{doctor_id}/profile",
            authenticated=False,
        )
    except ApiError as error:
        if error.status_code == 404:
            return render_template(
                "public_doctor_profile.html",
                profile=None,
                error_message="Doctor profile not found.",
            ), 404
        app.logger.error(
            "Could not load public doctor profile: doctor_id=%s status=%s",
            doctor_id,
            error.status_code,
        )
        return render_template(
            "public_doctor_profile.html",
            profile=None,
            error_message="Unable to load this doctor profile right now.",
        ), 502

    profile = payload.get("profile") if isinstance(payload, dict) else None
    if not isinstance(profile, dict):
        app.logger.error(
            "Public doctor profile API returned an invalid response: doctor_id=%s",
            doctor_id,
        )
        return render_template(
            "public_doctor_profile.html",
            profile=None,
            error_message="Unable to load this doctor profile right now.",
        ), 502
    return render_template("public_doctor_profile.html", profile=profile)


def patient_profile_payload():
    return {
        field: request.form.get(field, "").strip()
        for field in (
            "first_name",
            "last_name",
            "phone_number",
            "date_of_birth",
            "gender",
            "emergency_contact",
            "emergency_contact_name",
        )
    }


@app.post("/patient/profile")
@patient_required
def update_patient_profile():
    try:
        api_request(
            "PUT",
            "/patients/me",
            json_body=patient_profile_payload(),
        )
    except ApiError as error:
        return patient_api_error(error)
    flash("Patient profile saved.", "success")
    return redirect(url_for("patient_dashboard"))


@app.post("/patient/profile/delete")
@patient_required
def delete_patient_profile():
    try:
        api_request("DELETE", "/patients/me")
    except ApiError as error:
        return patient_api_error(error)
    flash("Patient profile cleared.", "success")
    return redirect(url_for("patient_dashboard"))


@app.post("/doctor/profile")
@doctor_required
@with_api_errors
def update_profile():
    fields = (
        "first_name",
        "last_name",
        "phone_number",
        "license_number",
        "experience_years",
        "qualification",
        "languages",
        "specializations",
        "hospital_name",
        "city",
        "address",
        "hospital_phone",
    )
    payload = {field: request.form.get(field, "").strip() for field in fields}
    if payload["experience_years"]:
        payload["experience_years"] = int(payload["experience_years"])
    payload = {key: value for key, value in payload.items() if value != ""}
    api_request("PUT", "/doctors/me", json_body=payload)
    flash("Profile saved.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/consultation-types")
@doctor_required
@with_api_errors
def create_consultation_type():
    api_request(
        "POST",
        "/doctors/me/consultation-types",
        json_body={
            "consultation_name": request.form["consultation_name"],
            "duration_minutes": int(request.form["duration_minutes"]),
            "capacity": int(request.form["capacity"]),
            "fee": request.form["fee"],
        },
    )
    flash("Consultation type added.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/consultation-types/<int:consultation_id>")
@doctor_required
@with_api_errors
def update_consultation_type(consultation_id):
    api_request(
        "PUT",
        f"/doctors/me/consultation-types/{consultation_id}",
        json_body={
            "consultation_name": request.form["consultation_name"],
            "duration_minutes": int(request.form["duration_minutes"]),
            "capacity": int(request.form["capacity"]),
            "fee": request.form["fee"],
        },
    )
    flash("Consultation type updated.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/consultation-types/<int:consultation_id>/delete")
@doctor_required
@with_api_errors
def delete_consultation_type(consultation_id):
    api_request("DELETE", f"/doctors/me/consultation-types/{consultation_id}")
    flash("Consultation type deleted.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/availability")
@doctor_required
@with_api_errors
def save_availability():
    consultation_ids = request.form.getlist("consultation_type_id")
    weekdays = request.form.getlist("day_of_week")
    starts = request.form.getlist("start_time")
    ends = request.form.getlist("end_time")
    patterns = []
    for consultation_id, weekday, start, end in zip(
        consultation_ids, weekdays, starts, ends
    ):
        if consultation_id and weekday and start and end:
            patterns.append(
                {
                    "consultation_type_id": int(consultation_id),
                    "day_of_week": weekday,
                    "start_time": start,
                    "end_time": end,
                }
            )
    api_request("PUT", "/doctors/me/availability", json_body={"patterns": patterns})
    flash("Weekly availability saved.", "success")
    return redirect(
        url_for("doctor_dashboard", view="availability-view")
    )


@app.post("/doctor/blackouts")
@doctor_required
@with_api_errors
def add_blackout():
    api_request(
        "POST",
        "/doctors/me/blackouts",
        json_body={
            "start_datetime": request.form.get("start_datetime", ""),
            "end_datetime": request.form.get("end_datetime", ""),
        },
    )
    flash("Blackout day added.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/blackouts/<int:blackout_id>/delete")
@doctor_required
@with_api_errors
def delete_blackout(blackout_id):
    api_request("DELETE", f"/doctors/me/blackouts/{blackout_id}")
    flash("Blackout day removed.", "success")
    return redirect(url_for("doctor_dashboard"))


@app.post("/doctor/photo-url")
@doctor_required
def photo_upload_url():
    content_type = request.form.get("content_type", "")
    try:
        result = api_request(
            "POST",
            "/doctors/me/photo",
            json_body={"content_type": content_type},
        )
        return jsonify(result)
    except ApiError as error:
        return jsonify({"message": str(error)}), error.status_code


@app.post("/doctor/photo-confirm")
@doctor_required
def confirm_photo_upload():
    key = request.form.get("key", "")
    try:
        result = api_request(
            "POST",
            "/doctors/me/photo/confirm",
            json_body={"key": key},
        )
        return jsonify(result)
    except ApiError as error:
        return jsonify({"message": str(error)}), error.status_code


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)