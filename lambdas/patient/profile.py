import json
import logging
import os
import re
from datetime import date

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
ssm = boto3.client("ssm")
parameter_cache = {}

PROFILE_FIELDS = {
    "first_name",
    "last_name",
    "phone_number",
    "date_of_birth",
    "gender",
    "emergency_contact",
    "emergency_contact_name",
}
IDENTITY_FIELDS = {"user_id", "patient_id"}
REQUIRED_PROFILE_FIELDS = {"first_name", "last_name", "phone_number", "email"}
PHONE_PATTERN = re.compile(r"^\+?[0-9() .-]{7,20}$")


class ValidationError(Exception):
    pass


def get_parameter(name, decrypt=False):
    if name not in parameter_cache:
        response = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        parameter_cache[name] = response["Parameter"]["Value"]
    return parameter_cache[name]


def get_connection():
    return pymysql.connect(
        host=get_parameter(f"/medislot/{ENVIRONMENT}/rds-host"),
        port=int(get_parameter(f"/medislot/{ENVIRONMENT}/rds-port")),
        user=get_parameter(f"/medislot/{ENVIRONMENT}/rds-user"),
        password=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-password",
            decrypt=True,
        ),
        database=get_parameter(f"/medislot/{ENVIRONMENT}/rds-database"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }


def parse_body(event):
    raw_body = event.get("body")
    if not raw_body:
        return {}
    try:
        body = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValidationError("Request body must be valid JSON.") from error
    if not isinstance(body, dict):
        raise ValidationError("Request body must be a JSON object.")
    return body


def validate_identity_fields(body, user_id, patient_id=None):
    if "user_id" in body and str(body["user_id"]) != str(user_id):
        return response(
            403,
            {"message": "You are not allowed to access this profile."},
        )
    if "patient_id" in body and (
        patient_id is None
        or str(body["patient_id"]) != str(patient_id)
    ):
        return response(
            403,
            {"message": "You are not allowed to access this profile."},
        )
    return None


def validate_profile(body):
    unknown_fields = set(body) - PROFILE_FIELDS - IDENTITY_FIELDS
    if unknown_fields:
        raise ValidationError("Request contains unsupported profile fields.")

    profile = {}
    for field in PROFILE_FIELDS:
        if field not in body:
            continue

        value = body[field]
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be a string.")
        value = value.strip()
        if not value:
            if field in {"first_name", "last_name", "phone_number"}:
                raise ValidationError(f"{field} is required.")
            profile[field] = None
            continue

        if field in {"first_name", "last_name"} and len(value) > 100:
            raise ValidationError(f"{field} must be 100 characters or fewer.")
        if field in {"phone_number", "emergency_contact"}:
            digit_count = sum(character.isdigit() for character in value)
            if not PHONE_PATTERN.fullmatch(value) or not 7 <= digit_count <= 15:
                raise ValidationError(f"{field} must be a valid phone number.")
        if field == "date_of_birth":
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError as error:
                raise ValidationError(
                    "date_of_birth must be a valid date in YYYY-MM-DD format."
                ) from error
            if parsed_date > date.today():
                raise ValidationError("date_of_birth cannot be in the future.")
            value = parsed_date.isoformat()
        if field == "gender" and len(value) > 20:
            raise ValidationError("gender must be 20 characters or fewer.")
        if field == "emergency_contact_name" and len(value) > 200:
            raise ValidationError(
                "emergency_contact_name must be 200 characters or fewer."
            )
        profile[field] = value

    if not profile:
        raise ValidationError("Provide at least one profile field to update.")
    return profile


def get_profile(cursor, user_id):
    cursor.execute(
        """
        SELECT p.patient_id, p.user_id, p.first_name, p.last_name,
               p.phone_number, u.email, p.date_of_birth, p.gender,
               p.emergency_contact, p.emergency_contact_name,
               p.profile_filled
        FROM users u
        LEFT JOIN patients p ON p.user_id = u.user_id
        WHERE u.user_id = %s AND u.role = 'PATIENT'
        """,
        (user_id,),
    )
    return cursor.fetchone()


def ensure_patient_record(cursor, user_id):
    cursor.execute(
        """
        INSERT INTO patients (user_id, first_name, last_name, profile_filled)
        SELECT user_id, NULL, NULL, FALSE
        FROM users
        WHERE user_id = %s AND role = 'PATIENT'
        ON DUPLICATE KEY UPDATE user_id = VALUES(user_id)
        """,
        (user_id,),
    )


def calculate_profile_filled(profile):
    return all(profile.get(field) for field in REQUIRED_PROFILE_FIELDS)


def lambda_handler(event, context):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    user_id = authorizer.get("userId")
    if not user_id:
        logger.warning(
            "Patient profile request missing identity: request_id=%s",
            getattr(context, "aws_request_id", "unknown"),
        )
        return response(401, {"message": "Authentication is required."})
    if authorizer.get("role") != "PATIENT":
        logger.warning(
            "Patient profile access denied: role=%s request_id=%s",
            authorizer.get("role", "unknown"),
            getattr(context, "aws_request_id", "unknown"),
        )
        return response(
            403,
            {"message": "You are not allowed to access this profile."},
        )
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return response(401, {"message": "Authentication is required."})

    method = event.get("httpMethod", "").upper()
    path = event.get("path", "").rstrip("/")
    if path == "/patients/me" and method in {"GET", "PUT", "DELETE"}:
        operation = method.lower()
    elif path != "/patients/me":
        return response(404, {"message": "Route not found."})
    else:
        return response(405, {"message": "Method not allowed."})

    logger.info(
        "Patient profile request: operation=%s user_id=%s request_id=%s",
        operation,
        user_id,
        getattr(context, "aws_request_id", "unknown"),
    )
    try:
        body = parse_body(event) if method in {"POST", "PUT", "DELETE"} else {}
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                ensure_patient_record(cursor, user_id)
                if operation == "get":
                    profile = get_profile(cursor, user_id)
                    if not profile or not profile["patient_id"]:
                        connection.rollback()
                        return response(404, {"message": "Patient profile not found."})
                    connection.commit()
                    return response(200, {"profile": profile})

                current = get_profile(cursor, user_id)
                identity_error = validate_identity_fields(
                    body,
                    user_id,
                    current["patient_id"] if current else None,
                )
                if identity_error:
                    connection.rollback()
                    return identity_error

                if not current:
                    connection.rollback()
                    return response(404, {"message": "Patient profile not found."})

                if operation == "put":
                    updates = validate_profile(body)
                    updated_profile = {
                        "first_name": current["first_name"],
                        "last_name": current["last_name"],
                        "phone_number": current["phone_number"],
                        "email": current["email"],
                        "date_of_birth": current["date_of_birth"],
                        "gender": current["gender"],
                        "emergency_contact": current["emergency_contact"],
                        "emergency_contact_name": current["emergency_contact_name"],
                    }
                    updated_profile.update(updates)
                    profile_filled = calculate_profile_filled(updated_profile)
                    cursor.execute(
                        """
                        INSERT INTO patients (
                            user_id, first_name, last_name, phone_number,
                            date_of_birth, gender, emergency_contact,
                            emergency_contact_name, profile_filled
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            first_name = VALUES(first_name),
                            last_name = VALUES(last_name),
                            phone_number = VALUES(phone_number),
                            date_of_birth = VALUES(date_of_birth),
                            gender = VALUES(gender),
                            emergency_contact = VALUES(emergency_contact),
                            emergency_contact_name = VALUES(emergency_contact_name),
                            profile_filled = VALUES(profile_filled)
                        """,
                        (
                            user_id,
                            updated_profile["first_name"],
                            updated_profile["last_name"],
                            updated_profile["phone_number"],
                            updated_profile["date_of_birth"],
                            updated_profile["gender"],
                            updated_profile["emergency_contact"],
                            updated_profile["emergency_contact_name"],
                            profile_filled,
                        ),
                    )
                    connection.commit()
                    saved_profile = get_profile(cursor, user_id)
                    logger.info(
                        "Patient profile updated: user_id=%s request_id=%s",
                        user_id,
                        getattr(context, "aws_request_id", "unknown"),
                    )
                    return response(200, {"profile": saved_profile})

                cursor.execute(
                    """
                    UPDATE patients
                    SET first_name = NULL,
                        last_name = NULL,
                        phone_number = NULL,
                        date_of_birth = NULL,
                        gender = NULL,
                        emergency_contact = NULL,
                        emergency_contact_name = NULL,
                        profile_filled = FALSE
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                connection.commit()
                logger.info(
                    "Patient profile cleared: user_id=%s request_id=%s",
                    user_id,
                    getattr(context, "aws_request_id", "unknown"),
                )
                return response(200, {"message": "Patient profile cleared."})
        finally:
            connection.close()
    except ValidationError as error:
        return response(400, {"message": str(error)})
    except pymysql.err.IntegrityError as error:
        if getattr(error, "args", [None])[0] == 1451:
            return response(
                409,
                {"message": "Profile cannot be deleted while appointments exist."},
            )
        if getattr(error, "args", [None])[0] == 1062:
            return response(409, {"message": "Email address is already in use."})
        logger.exception("Patient profile database constraint failed.")
        return response(500, {"message": "Could not save the patient profile."})
    except pymysql.MySQLError:
        logger.exception(
            "Patient profile database operation failed: user_id=%s request_id=%s",
            user_id,
            getattr(context, "aws_request_id", "unknown"),
        )
        return response(500, {"message": "Could not complete the profile request."})
