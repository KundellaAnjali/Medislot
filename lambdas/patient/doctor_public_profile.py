import logging
import re

import pymysql

from profile import get_connection, response


logger = logging.getLogger()
logger.setLevel(logging.INFO)

DOCTOR_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
MAX_DOCTOR_ID = 2**31 - 1


def get_doctor_id(event):
    path_parameters = event.get("pathParameters") or {}
    value = path_parameters.get("doctor_id")
    if value is None:
        path = event.get("path", "").rstrip("/")
        match = re.search(r"/doctors/([^/]+)/profile$", path)
        value = match.group(1) if match else None

    if not isinstance(value, str) or not DOCTOR_ID_PATTERN.fullmatch(value):
        return None

    doctor_id = int(value)
    if doctor_id > MAX_DOCTOR_ID:
        return None
    return doctor_id


def get_public_profile(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            TRIM(CONCAT_WS(' ', d.first_name, d.last_name)) AS doctor_name,
            GROUP_CONCAT(
                DISTINCT s.specialization_name
                ORDER BY s.specialization_name
                SEPARATOR ', '
            ) AS specialization,
            d.qualification AS qualifications,
            d.languages,
            h.hospital_name AS hospital,
            h.city,
            d.is_verified AS verification_status
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        LEFT JOIN doctor_specializations ds ON ds.doctor_id = d.doctor_id
        LEFT JOIN specializations s
          ON s.specialization_id = ds.specialization_id
        WHERE d.doctor_id = %s
        GROUP BY
            d.doctor_id,
            d.first_name,
            d.last_name,
            d.qualification,
            d.languages,
            h.hospital_name,
            h.city,
            d.is_verified
        """,
        (doctor_id,),
    )
    return cursor.fetchone()


def get_consultation_details(cursor, doctor_id):
    cursor.execute(
        """
        SELECT
            ct.consultation_name AS consultation_type,
            ct.duration_minutes AS duration,
            ct.capacity,
            dct.fee
        FROM doctor_consulting_types dct
        JOIN consultation_types ct
          ON ct.consultation_type_id = dct.consultation_type_id
        WHERE dct.doctor_id = %s AND dct.is_active = TRUE
        ORDER BY ct.consultation_name
        """,
        (doctor_id,),
    )
    return cursor.fetchall()


def lambda_handler(event, context):
    method = event.get("httpMethod", "").upper()
    resource = event.get("resource", "")
    path = event.get("path", "").rstrip("/")
    is_profile_route = (
        resource == "/doctors/{doctor_id}/profile"
        or re.search(r"/doctors/[^/]+/profile$", path) is not None
    )
    if not is_profile_route:
        return response(404, {"message": "Route not found."})
    if method != "GET":
        return response(405, {"message": "Method not allowed."})

    doctor_id = get_doctor_id(event)
    if doctor_id is None:
        return response(400, {"message": "doctor_id must be a positive integer."})

    request_id = getattr(context, "aws_request_id", "unknown")
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            profile = get_public_profile(cursor, doctor_id)
            if not profile:
                logger.info(
                    "Public doctor profile not found: request_id=%s",
                    request_id,
                )
                return response(404, {"message": "Doctor profile not found."})

            profile["verification_status"] = bool(
                profile["verification_status"]
            )
            profile["consultation_details"] = get_consultation_details(
                cursor,
                doctor_id,
            )

        logger.info("Public doctor profile retrieved: request_id=%s", request_id)
        return response(200, {"profile": profile})
    except pymysql.MySQLError:
        logger.exception(
            "Public doctor profile database request failed: request_id=%s",
            request_id,
        )
        return response(500, {"message": "Could not load doctor profile right now."})
    finally:
        if connection:
            connection.close()
