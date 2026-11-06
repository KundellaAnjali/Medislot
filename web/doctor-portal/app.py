import json
import logging
import os
from time import perf_counter
from datetime import datetime, timezone
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
    SESSION_FILE_DIR = "/tmp/flask_session",
    SESSION_PERMANENT=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
Session(app)

API_BASE_URL = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/") + "/"


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


def doctor_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "DOCTOR":
            return redirect(url_for("auth", mode="login", role="DOCTOR"))
        return handler(*args, **kwargs)

    return wrapped


def patient_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "PATIENT":
            return redirect(url_for("auth", mode="login", role="PATIENT"))
        return handler(*args, **kwargs)

    return wrapped


def api_request(method, path, *, json_body=None, params=None, authenticated=True):
    headers = {}
    if authenticated:
        headers["Authorization"] = f"Bearer {session['access_token']}"
    try:
        result = requests.request(
            method,
            urljoin(API_BASE_URL, path.lstrip("/")),
            headers=headers,
            json=json_body,
            params=params,
            timeout=15,
        )
    except requests.RequestException as error:
        app.logger.exception("MediSlot API request failed: method=%s path=%s", method, path)
        raise ApiError("MediSlot API is unavailable") from error
    try:
        payload = result.json()
    except ValueError:
        payload = {}
    if not result.ok:
        app.logger.warning(
            "MediSlot API returned an error: method=%s path=%s status=%s",
            method,
            path,
            result.status_code,
        )
        raise ApiError(payload.get("message", "MediSlot API request failed"), result.status_code)
    app.logger.info(
        "MediSlot API request completed: method=%s path=%s status=%s",
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
                session.clear()
                flash("Your session expired. Please sign in again.", "error")
                return redirect(url_for("auth", mode="login"))
            flash(str(error), "error")
            return redirect(request.referrer or url_for("doctor_dashboard"))

    return wrapped


@app.get("/")
def home():
    if session.get("role") == "DOCTOR":
        return redirect(url_for("doctor_dashboard"))
    if session.get("role") == "PATIENT":
        return redirect(url_for("patient_dashboard"))
    return redirect(url_for("auth"))

@app.get("/debug-session")
def debug_session():
    return dict(session)

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
    profile = api_request("GET", "/doctors/me")
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
    )


@app.get("/doctor")
def old_doctor_dashboard():
    return redirect(url_for("doctor_dashboard"))


@app.get("/dashboard/patient")
@patient_required
def patient_dashboard():
    return render_template("patient_dashboard.html")


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
    return redirect(url_for("doctor_dashboard"))


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