import hmac
import json
import os
import secrets
from datetime import datetime
from functools import wraps
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ["FLASK_SECRET_KEY"],
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR = "/tmp/flask_session"
    SESSION_PERMANENT=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
Session(app)

API_BASE_URL = os.environ["MEDISLOT_API_BASE_URL"].rstrip("/") + "/"


class ApiError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.before_request
def verify_csrf():
    if request.method == "POST":
        submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        if not expected or not hmac.compare_digest(submitted, expected):
            return "Invalid request token", 400


def doctor_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not session.get("access_token") or session.get("role") != "DOCTOR":
            return redirect(url_for("login"))
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
        raise ApiError("MediSlot API is unavailable") from error
    try:
        payload = result.json()
    except ValueError:
        payload = {}
    if not result.ok:
        raise ApiError(payload.get("message", "MediSlot API request failed"), result.status_code)
    return payload


def with_api_errors(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except ApiError as error:
            if error.status_code == 401:
                session.clear()
                flash("Your session expired. Please sign in again.", "error")
                return redirect(url_for("login"))
            flash(str(error), "error")
            return redirect(request.referrer or url_for("dashboard"))

    return wrapped


@app.get("/")
def home():
    if session.get("role") == "DOCTOR":
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/doctor/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Enter your email and password.", "error")
        return render_template("login.html"), 400
    try:
        result = api_request(
            "POST",
            "/auth/login",
            json_body={"email": email, "password": password},
            authenticated=False,
        )
    except ApiError as error:
        flash(str(error), "error")
        return render_template("login.html"), error.status_code
    if result.get("role") != "DOCTOR":
        flash("This portal is for doctor accounts.", "error")
        return render_template("login.html"), 403
    session.clear()
    session["access_token"] = result["access_token"]
    session["refresh_token"] = result["refresh_token"]
    session["role"] = result["role"]
    csrf_token()
    return redirect(url_for("dashboard"))


@app.post("/doctor/logout")
@doctor_required
def logout():
    try:
        api_request(
            "POST",
            "/auth/logout",
            json_body={"refresh_token": session.get("refresh_token")},
            authenticated=False,
        )
    except ApiError:
        pass
    session.clear()
    return redirect(url_for("login"))


@app.get("/doctor")
@doctor_required
@with_api_errors
def dashboard():
    profile = api_request("GET", "/doctors/me")
    consultations = api_request("GET", "/doctors/me/consultation-types").get(
        "consultation_types", []
    )
    availability = api_request("GET", "/doctors/me/availability").get("patterns", [])
    timezone_name = profile.get("timezone_name") or "UTC"
    try:
        doctor_timezone = ZoneInfo(timezone_name)
        schedule_date = request.args.get("date") or datetime.now(doctor_timezone).date().isoformat()
        schedule = api_request(
            "GET", "/doctors/me/schedule", params={"date": schedule_date}
        )
    except ApiError as error:
        if error.status_code == 404:
            schedule = {"slots": [], "date": schedule_date, "timezone": timezone_name}
        else:
            raise
    return render_template(
        "dashboard.html",
        profile=profile,
        consultations=consultations,
        patterns=availability,
        schedule=schedule,
    )


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
        "timezone",
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
    return redirect(url_for("dashboard"))


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
    return redirect(url_for("dashboard"))


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
    return redirect(url_for("dashboard"))


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)