import hashlib
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
import pymysql
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
SLOT_TABLE_NAME = os.environ["SLOTS_TABLE_NAME"]
PHOTO_BUCKET_NAME = os.environ["PHOTO_BUCKET_NAME"]

ssm = boto3.client("ssm")
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
slots_table = dynamodb.Table(SLOT_TABLE_NAME)
parameter_cache = {}

CONSULTATION_RULES = {
    "Quick Consult": (15, 1),
    "Standard Consult": (30, 1),
    "Detailed Consult": (60, 1),
    "Health Camp Session": (60, None),
}


def get_parameter(name):
    if name not in parameter_cache:
        response = ssm.get_parameter(Name=name)
        parameter_cache[name] = response["Parameter"]["Value"]
    return parameter_cache[name]


def get_connection():
    return pymysql.connect(
        host=get_parameter(f"/medislot/{ENVIRONMENT}/rds-host"),
        port=int(get_parameter(f"/medislot/{ENVIRONMENT}/rds-port")),
        user=get_parameter(f"/medislot/{ENVIRONMENT}/rds-user"),
        password=ssm.get_parameter(
            Name=f"/medislot/{ENVIRONMENT}/rds-password",
            WithDecryption=True,
        )["Parameter"]["Value"],
        database=get_parameter(f"/medislot/{ENVIRONMENT}/rds-database"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(payload, default=str),
    }


def request_body(event):
    raw_body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw_body = base64.b64decode(raw_body).decode("utf-8")
    body = json.loads(raw_body)
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def get_actor(event):
    authorizer = event.get("requestContext", {}).get("authorizer", {})
    role = authorizer.get("role")
    user_id = authorizer.get("userId")
    if role != "DOCTOR":
        raise PermissionError("Doctor access required")
    if not user_id:
        raise PermissionError("Doctor identity is missing")
    return int(user_id)


def doctor_id_for_user(cursor, user_id):
    cursor.execute(
        "SELECT doctor_id FROM doctors WHERE user_id = %s",
        (user_id,),
    )
    doctor = cursor.fetchone()
    if not doctor:
        raise LookupError("Doctor profile not found")
    return doctor["doctor_id"]


def get_profile(cursor, doctor_id):
    cursor.execute(
        """
        SELECT d.doctor_id, u.email, d.first_name, d.last_name, d.phone_number,
               d.license_number, d.experience_years, d.qualification,
               d.profile_image_key, d.languages, d.is_verified,
               h.hospital_name, h.city, h.address, h.phone_number AS hospital_phone
        FROM doctors d
        JOIN users u ON u.user_id = d.user_id
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE d.doctor_id = %s
        """,
        (doctor_id,),
    )
    profile = cursor.fetchone()
    cursor.execute(
        """
        SELECT s.specialization_id, s.specialization_name
        FROM doctor_specializations ds
        JOIN specializations s ON s.specialization_id = ds.specialization_id
        WHERE ds.doctor_id = %s
        ORDER BY s.specialization_name
        """,
        (doctor_id,),
    )
    profile["specializations"] = cursor.fetchall()
    cursor.execute(
        "SELECT specialization_id, specialization_name FROM specializations "
        "ORDER BY specialization_name"
    )
    profile["available_specializations"] = cursor.fetchall()
    profile["specialization_names"] = [
        item["specialization_name"] for item in profile["specializations"]
    ]
    profile["is_verified"] = bool(profile["is_verified"])
    profile["profile_image_url"] = (
        s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": PHOTO_BUCKET_NAME, "Key": profile["profile_image_key"]},
            ExpiresIn=3600,
        )
        if profile["profile_image_key"]
        else None
    )
    return profile


def refresh_doctor_verification(cursor, doctor_id):
    cursor.execute(
        """
        SELECT d.first_name, d.last_name, d.phone_number, d.license_number,
               d.experience_years, d.qualification, d.profile_image_key,
               d.languages, h.hospital_name, h.city, h.address,
               (SELECT COUNT(*) FROM doctor_specializations ds
                WHERE ds.doctor_id = d.doctor_id) AS specialization_count
        FROM doctors d
        LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
        WHERE d.doctor_id = %s
        """,
        (doctor_id,),
    )
    profile = cursor.fetchone()
    required_text = (
        "first_name",
        "last_name",
        "phone_number",
        "license_number",
        "qualification",
        "languages",
        "profile_image_key",
        "hospital_name",
        "city",
        "address",
    )
    complete = all(
        isinstance(profile[field], str) and profile[field].strip()
        for field in required_text
    )
    complete = (
        complete
        and profile["experience_years"] is not None
        and int(profile["experience_years"]) >= 0
        and int(profile["specialization_count"]) > 0
    )
    cursor.execute(
        "UPDATE doctors SET is_verified = %s WHERE doctor_id = %s",
        (complete, doctor_id),
    )
    return complete


def require_verified_doctor(cursor, doctor_id):
    cursor.execute(
        "SELECT is_verified FROM doctors WHERE doctor_id = %s",
        (doctor_id,),
    )
    doctor = cursor.fetchone()
    if not doctor or not doctor["is_verified"]:
        raise PermissionError("Before profile completion, you are not allowed to do this.")


def validate_consultation(body):
    name = body.get("consultation_name")
    if name not in CONSULTATION_RULES:
        raise ValueError("Unsupported consultation type")
    expected_duration, fixed_capacity = CONSULTATION_RULES[name]
    duration = int(body.get("duration_minutes", expected_duration))
    capacity = int(body.get("capacity", fixed_capacity or 2))
    if duration != expected_duration:
        raise ValueError("Duration does not match the consultation type")
    if fixed_capacity is not None and capacity != fixed_capacity:
        raise ValueError("This consultation type has a capacity of 1")
    if fixed_capacity is None and not 2 <= capacity <= 10:
        raise ValueError("Health Camp capacity must be between 2 and 10")
    try:
        fee = Decimal(str(body["fee"]))
    except (KeyError, InvalidOperation):
        raise ValueError("A valid fee is required") from None
    if not fee.is_finite() or fee < 0 or fee.as_tuple().exponent < -2:
        raise ValueError("Fee must be a non-negative amount with up to 2 decimals")
    return name, duration, capacity, fee


def parse_sql_time(value):
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    seconds = int(value.total_seconds())
    return (datetime.min + timedelta(seconds=seconds)).time()


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def utc_day_bounds(day):
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end


def parse_utc_datetime(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def set_slots_for_blackout(
    doctor_id,
    start_utc,
    end_utc,
    blackouts,
    active_consultation_ids=None,
):
    if start_utc >= end_utc:
        return
    query_start = utc_iso(start_utc - timedelta(hours=1))
    query_end = utc_iso(end_utc)
    query_args = {
        "IndexName": "DoctorStartIndex",
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id)
        & Key("start_time").between(query_start, query_end),
    }
    while True:
        result = slots_table.query(**query_args)
        for slot in result.get("Items", []):
            slot_start = parse_utc_datetime(slot["start_time"])
            slot_end = parse_utc_datetime(slot["end_time"])
            if slot_start >= end_utc or slot_end <= start_utc:
                continue
            blackout_overlap = any(
                item["start_datetime"].replace(tzinfo=timezone.utc) < slot_end
                and item["end_datetime"].replace(tzinfo=timezone.utc) > slot_start
                for item in blackouts
            )
            if blackout_overlap:
                new_status = 0
            elif active_consultation_ids is not None and int(
                slot["consultation_type_id"]
            ) in active_consultation_ids:
                new_status = 1
            else:
                continue
            slots_table.update_item(
                Key={"slot_id": slot["slot_id"]},
                UpdateExpression="SET #status = :status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": new_status},
            )
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key


def set_consultation_slots_status(doctor_id, consultation_id, status):
    query_args = {
        "IndexName": "DoctorStartIndex",
        "KeyConditionExpression": Key("doctor_id").eq(doctor_id),
    }
    while True:
        result = slots_table.query(**query_args)
        for slot in result.get("Items", []):
            if int(slot["consultation_type_id"]) == consultation_id:
                slots_table.update_item(
                    Key={"slot_id": slot["slot_id"]},
                    UpdateExpression="SET #status = :status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":status": status},
                )
        last_key = result.get("LastEvaluatedKey")
        if not last_key:
            break
        query_args["ExclusiveStartKey"] = last_key


def route_doctor_api(event):
    user_id = get_actor(event)
    method = event.get("httpMethod", "GET").upper()
    proxy = (event.get("pathParameters") or {}).get("proxy","")
    path = "/me"
    if proxy:
        path += "/" + proxy.strip("/")
    query = event.get("queryStringParameters") or {}
    body = request_body(event) if event.get("body") else {}

    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            doctor_id = doctor_id_for_user(cursor, user_id)
            parts = path.strip("/").split("/")

            if parts == ["me"] and method == "GET":
                return response(200, get_profile(cursor, doctor_id))

            if parts == ["me"] and method == "PUT":
                update_doctor_profile(connection, cursor, doctor_id, body)
                return response(200, get_profile(cursor, doctor_id))

            if parts == ["me", "photo"] and method == "POST":
                result = create_photo_upload(cursor, doctor_id, body)
                return result

            if parts == ["me", "photo", "confirm"] and method == "POST":
                result = confirm_photo_upload(connection, cursor, doctor_id, body)
                return result

            if parts[:2] == ["me", "consultation-types"]:
                if method != "GET":
                    require_verified_doctor(cursor, doctor_id)
                return consultation_types_route(
                    connection, cursor, doctor_id, method, parts[2:], body
                )

            if parts == ["me", "availability"]:
                if method != "GET":
                    require_verified_doctor(cursor, doctor_id)
                return availability_route(
                    connection, cursor, doctor_id, method, body
                )

            if parts[:2] == ["me", "blackouts"]:
                if method != "GET":
                    require_verified_doctor(cursor, doctor_id)
                return blackouts_route(
                    connection, cursor, doctor_id, method, parts[2:], body
                )

            if parts == ["me", "schedule"] and method == "GET":
                return get_day_schedule(cursor, doctor_id, query)

            raise LookupError("Doctor endpoint not found")
    finally:
        connection.close()


def update_doctor_profile(connection, cursor, doctor_id, body):
    allowed = {
        "first_name",
        "last_name",
        "phone_number",
        "license_number",
        "experience_years",
        "qualification",
        "languages",
    }
    values = {key: body[key] for key in allowed if key in body}
    if "experience_years" in values:
        values["experience_years"] = int(values["experience_years"])
        if values["experience_years"] < 0:
            raise ValueError("Experience years cannot be negative")
    hospital_fields = {"hospital_name", "city", "address", "hospital_phone"}
    if hospital_fields.intersection(body):
        name = str(body.get("hospital_name", "")).strip()
        if not name:
            raise ValueError("hospital_name is required when updating hospital details")
        cursor.execute(
            """
            SELECT hospital_id FROM hospitals
            WHERE hospital_name = %s AND city <=> %s AND address <=> %s
            LIMIT 1
            """,
            (name, body.get("city"), body.get("address")),
        )
        hospital = cursor.fetchone()
        if hospital:
            hospital_id = hospital["hospital_id"]
        else:
            cursor.execute(
                """
                INSERT INTO hospitals (hospital_name, city, address, phone_number)
                VALUES (%s, %s, %s, %s)
                """,
                (name, body.get("city"), body.get("address"), body.get("hospital_phone")),
            )
            hospital_id = cursor.lastrowid
        values["hospital_id"] = hospital_id

    if values:
        assignments = ", ".join(f"{field} = %s" for field in values)
        cursor.execute(
            f"UPDATE doctors SET {assignments} WHERE doctor_id = %s",
            (*values.values(), doctor_id),
        )

    if "specializations" in body:
        submitted_names = body["specializations"]
        if isinstance(submitted_names, str):
            submitted_names = submitted_names.split(",")
        if not isinstance(submitted_names, list):
            raise ValueError("specializations must be a comma-separated string or list")
        names_by_lowercase = {}
        for item in submitted_names:
            name = str(item).strip()
            if name:
                names_by_lowercase[name.casefold()] = name
        cursor.execute(
            "DELETE FROM doctor_specializations WHERE doctor_id = %s",
            (doctor_id,),
        )
        for name in names_by_lowercase.values():
            cursor.execute(
                """
                INSERT INTO specializations (specialization_name)
                VALUES (%s)
                ON DUPLICATE KEY UPDATE
                    specialization_id = LAST_INSERT_ID(specialization_id)
                """,
                (name,),
            )
            cursor.execute(
                """
                INSERT INTO doctor_specializations (doctor_id, specialization_id)
                VALUES (%s, %s)
                """,
                (doctor_id, cursor.lastrowid),
            )
    elif "specialization_ids" in body:
        specialization_ids = body["specialization_ids"]
        if not isinstance(specialization_ids, list):
            raise ValueError("specialization_ids must be a list")
        normalized_ids = sorted({int(item) for item in specialization_ids})
        cursor.execute(
            "DELETE FROM doctor_specializations WHERE doctor_id = %s",
            (doctor_id,),
        )
        for specialization_id in normalized_ids:
            cursor.execute(
                """
                INSERT INTO doctor_specializations (doctor_id, specialization_id)
                VALUES (%s, %s)
                """,
                (doctor_id, specialization_id),
            )
    refresh_doctor_verification(cursor, doctor_id)
    connection.commit()


def create_photo_upload(cursor, doctor_id, body):
    content_type = body.get("content_type")
    extensions = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }
    if content_type not in extensions:
        raise ValueError("content_type must be image/jpeg, image/png, or image/webp")
    key = f"doctors/{doctor_id}/profile.{extensions[content_type]}"
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": PHOTO_BUCKET_NAME,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=300,
    )
    return response(200, {"upload_url": upload_url, "key": key, "expires_in": 300})


def confirm_photo_upload(connection, cursor, doctor_id, body):
    key = body.get("key", "")
    if not key.startswith(f"doctors/{doctor_id}/profile.") or key.rsplit(".", 1)[-1] not in {
        "jpg", "png", "webp"
    }:
        raise ValueError("Invalid profile photo key")
    try:
        metadata = s3.head_object(Bucket=PHOTO_BUCKET_NAME, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
            raise ValueError("Upload the profile photo before confirming it") from None
        raise
    if metadata.get("ContentLength", 0) <= 0 or metadata["ContentLength"] > 5 * 1024 * 1024:
        raise ValueError("Profile photo must be between 1 byte and 5 MB")
    if metadata.get("ContentType") not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("Unsupported profile photo type")
    cursor.execute(
        "UPDATE doctors SET profile_image_key = %s WHERE doctor_id = %s",
        (key, doctor_id),
    )
    verified = refresh_doctor_verification(cursor, doctor_id)
    connection.commit()
    return response(200, {"message": "Profile photo saved", "is_verified": verified})


def get_or_create_consultation_type(cursor, name, duration, capacity):
    cursor.execute(
        """
        SELECT consultation_type_id FROM consultation_types
        WHERE consultation_name = %s AND duration_minutes = %s AND capacity = %s
        ORDER BY consultation_type_id LIMIT 1
        """,
        (name, duration, capacity),
    )
    row = cursor.fetchone()
    if row:
        return row["consultation_type_id"]
    cursor.execute(
        """
        INSERT INTO consultation_types
            (consultation_name, duration_minutes, capacity)
        VALUES (%s, %s, %s)
        """,
        (name, duration, capacity),
    )
    return cursor.lastrowid


def consultation_types_route(connection, cursor, doctor_id, method, tail, body):
    if not tail and method == "GET":
        cursor.execute(
            """
            SELECT ct.consultation_type_id, ct.consultation_name,
                   ct.duration_minutes, ct.capacity, dct.fee, dct.is_active
            FROM doctor_consulting_types dct
            JOIN consultation_types ct
              ON ct.consultation_type_id = dct.consultation_type_id
            WHERE dct.doctor_id = %s
            ORDER BY ct.consultation_type_id
            """,
            (doctor_id,),
        )
        return response(200, {"consultation_types": cursor.fetchall()})

    if not tail and method == "POST":
        name, duration, capacity, fee = validate_consultation(body)
        consultation_id = get_or_create_consultation_type(
            cursor, name, duration, capacity
        )
        cursor.execute(
            """
            INSERT INTO doctor_consulting_types
                (consultation_type_id, doctor_id, fee, is_active)
            VALUES (%s, %s, %s, TRUE)
            ON DUPLICATE KEY UPDATE fee = VALUES(fee), is_active = TRUE
            """,
            (consultation_id, doctor_id, fee),
        )
        connection.commit()
        return response(201, {"consultation_type_id": consultation_id})

    if len(tail) == 1:
        consultation_id = int(tail[0])
        cursor.execute(
            """
                        SELECT ct.consultation_name, ct.duration_minutes, ct.capacity,
                                     dct.fee, dct.is_active
                        FROM doctor_consulting_types dct
                        JOIN consultation_types ct
                            ON ct.consultation_type_id = dct.consultation_type_id
                        WHERE dct.consultation_type_id = %s AND dct.doctor_id = %s
            """,
            (consultation_id, doctor_id),
        )
        current = cursor.fetchone()
        if not current:
            raise LookupError("Consultation type not found")
        if method == "PUT":
            merged = {**current, **body}
            name, duration, capacity, fee = validate_consultation(merged)
            target_id = get_or_create_consultation_type(
                cursor, name, duration, capacity
            )
            cursor.execute(
                """
                INSERT INTO doctor_consulting_types
                    (consultation_type_id, doctor_id, fee, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON DUPLICATE KEY UPDATE fee = VALUES(fee), is_active = TRUE
                """,
                (target_id, doctor_id, fee),
            )
            if target_id != consultation_id:
                cursor.execute(
                    """
                    UPDATE availability_patterns
                    SET consultation_type_id = %s
                    WHERE doctor_id = %s AND consultation_type_id = %s
                    """,
                    (target_id, doctor_id, consultation_id),
                )
            if target_id != consultation_id:
                cursor.execute(
                    """
                    UPDATE doctor_consulting_types SET is_active = FALSE
                    WHERE consultation_type_id = %s AND doctor_id = %s
                    """,
                    (consultation_id, doctor_id),
                )
            connection.commit()
            if target_id != consultation_id:
                set_consultation_slots_status(doctor_id, consultation_id, 0)
            return response(200, {"message": "Consultation type updated"})
        if method == "DELETE":
            cursor.execute(
                "DELETE FROM availability_patterns WHERE consultation_type_id = %s AND doctor_id = %s",
                (consultation_id, doctor_id),
            )
            cursor.execute(
                """
                UPDATE doctor_consulting_types SET is_active = FALSE
                WHERE consultation_type_id = %s AND doctor_id = %s
                """,
                (consultation_id, doctor_id),
            )
            connection.commit()
            set_consultation_slots_status(doctor_id, consultation_id, 0)
            return response(200, {"message": "Consultation type deactivated"})
    raise LookupError("Consultation type endpoint not found")


def availability_route(connection, cursor, doctor_id, method, body):
    if method == "GET":
        cursor.execute(
            """
            SELECT ap.pattern_id, ap.consultation_type_id, ap.day_of_week,
                   ap.start_time, ap.end_time
            FROM availability_patterns ap
                        JOIN doctor_consulting_types dct
                            ON dct.consultation_type_id = ap.consultation_type_id
                         AND dct.doctor_id = ap.doctor_id
                        WHERE ap.doctor_id = %s
            ORDER BY ap.day_of_week, ap.start_time
            """,
            (doctor_id,),
        )
        patterns = cursor.fetchall()
        for pattern in patterns:
            pattern["start_time"] = parse_sql_time(pattern["start_time"]).strftime("%H:%M")
            pattern["end_time"] = parse_sql_time(pattern["end_time"]).strftime("%H:%M")
        return response(200, {"patterns": patterns})
    if method != "PUT":
        raise LookupError("Availability endpoint not found")

    patterns = body.get("patterns")
    if not isinstance(patterns, list):
        raise ValueError("patterns must be a list")
    normalized = []
    for pattern in patterns:
        try:
            consultation_id = int(pattern["consultation_type_id"])
            day_name = str(pattern["day_of_week"]).upper()
            start = time.fromisoformat(pattern["start_time"])
            end = time.fromisoformat(pattern["end_time"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each pattern needs a consultation type, weekday, start, and end") from None
        if day_name not in {
            "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
            "FRIDAY", "SATURDAY", "SUNDAY",
        }:
            raise ValueError("day_of_week must be a weekday name")
        if start >= end:
            raise ValueError("Pattern end_time must be after start_time")
        if any(
            existing_day == day_name
            and start < existing_end
            and end > existing_start
            for _, existing_day, existing_start, existing_end in normalized
        ):
            raise ValueError(
                f"Hours overlap an existing block on {day_name.title()}; choose a different time."
            )
        cursor.execute(
            """
                        SELECT consultation_type_id FROM doctor_consulting_types
                        WHERE consultation_type_id = %s AND doctor_id = %s
                            AND is_active = TRUE
            """,
            (consultation_id, doctor_id),
        )
        if not cursor.fetchone():
            raise ValueError("Consultation type does not belong to this doctor")
        normalized.append((consultation_id, day_name, start, end))

    cursor.execute(
        """
                DELETE FROM availability_patterns
                WHERE doctor_id = %s
        """,
        (doctor_id,),
    )
    for consultation_id, day_name, start, end in normalized:
        cursor.execute(
            """
            INSERT INTO availability_patterns
                (doctor_id, consultation_type_id, day_of_week, start_time, end_time)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (doctor_id, consultation_id, day_name, start, end),
        )
    connection.commit()
    return response(200, {"patterns_saved": len(normalized)})


def blackouts_route(connection, cursor, doctor_id, method, tail, body):
    if not tail and method == "GET":
        cursor.execute(
            """
            SELECT blackout_id, start_datetime, end_datetime
            FROM blackout_dates WHERE doctor_id = %s ORDER BY start_datetime
            """,
            (doctor_id,),
        )
        blackouts = cursor.fetchall()
        for blackout in blackouts:
            blackout["start_display"] = blackout["start_datetime"].strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            blackout["end_display"] = blackout["end_datetime"].strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        return response(200, {"blackouts": blackouts})
    if not tail and method == "POST":
        if body.get("start_datetime") and body.get("end_datetime"):
            start_utc = parse_utc_datetime(body["start_datetime"])
            end_utc = parse_utc_datetime(body["end_datetime"])
        else:
            start_day = date.fromisoformat(
                body.get("start_date", body.get("date", ""))
            )
            end_day = date.fromisoformat(
                body.get("end_date", body.get("date", ""))
            )
            if end_day < start_day:
                raise ValueError("end_date must be on or after start_date")
            start_utc, _ = utc_day_bounds(start_day)
            _, end_utc = utc_day_bounds(end_day)
        if end_utc <= start_utc:
            raise ValueError("Blackout end must be after its start")
        cursor.execute(
            """
            INSERT INTO blackout_dates (doctor_id, start_datetime, end_datetime)
            VALUES (%s, %s, %s)
            """,
            (
                doctor_id,
                start_utc.replace(tzinfo=None),
                end_utc.replace(tzinfo=None),
            ),
        )
        blackout_id = cursor.lastrowid
        connection.commit()
        cursor.execute(
            "SELECT start_datetime, end_datetime FROM blackout_dates WHERE doctor_id = %s",
            (doctor_id,),
        )
        all_blackouts = cursor.fetchall()
        set_slots_for_blackout(
            doctor_id,
            start_utc,
            end_utc,
            all_blackouts,
        )
        return response(201, {"blackout_id": blackout_id})
    if len(tail) == 1 and method == "DELETE":
        blackout_id = int(tail[0])
        cursor.execute(
            """
            SELECT start_datetime, end_datetime FROM blackout_dates
            WHERE blackout_id = %s AND doctor_id = %s
            """,
            (blackout_id, doctor_id),
        )
        removed_blackout = cursor.fetchone()
        if not removed_blackout:
            raise LookupError("Blackout date not found")
        cursor.execute(
            "DELETE FROM blackout_dates WHERE blackout_id = %s AND doctor_id = %s",
            (blackout_id, doctor_id),
        )
        connection.commit()
        removed_start = removed_blackout["start_datetime"]
        removed_end = removed_blackout["end_datetime"]
        first_day = removed_start.date()
        last_day = (removed_end - timedelta(seconds=1)).date()
        cursor.execute(
            "SELECT start_datetime, end_datetime FROM blackout_dates WHERE doctor_id = %s",
            (doctor_id,),
        )
        remaining_blackouts = cursor.fetchall()
        cursor.execute(
            """
            SELECT consultation_type_id FROM doctor_consulting_types
            WHERE doctor_id = %s AND is_active = TRUE
            """,
            (doctor_id,),
        )
        active_consultation_ids = {
            int(item["consultation_type_id"]) for item in cursor.fetchall()
        }
        removed_start_utc = removed_blackout["start_datetime"].replace(
            tzinfo=timezone.utc
        )
        removed_end_utc = removed_blackout["end_datetime"].replace(
            tzinfo=timezone.utc
        )
        set_slots_for_blackout(
            doctor_id,
            max(removed_start_utc, datetime.now(timezone.utc)),
            removed_end_utc,
            remaining_blackouts,
            active_consultation_ids,
        )
        return response(200, {"message": "Blackout date deleted"})
    raise LookupError("Blackout endpoint not found")


def get_day_schedule(cursor, doctor_id, query):
    if not query.get("date"):
        raise ValueError("date query parameter is required (YYYY-MM-DD)")
    local_day = date.fromisoformat(query["date"])
    start_utc, end_utc = utc_day_bounds(local_day)
    result = slots_table.query(
        IndexName="DoctorStartIndex",
        KeyConditionExpression=Key("doctor_id").eq(doctor_id)
        & Key("start_time").between(
            utc_iso(start_utc), utc_iso(end_utc - timedelta(seconds=1))
        ),
    )
    slots = result.get("Items", [])
    slot_ids = [int(item["slot_id"]) for item in slots]
    appointments_by_slot = {}
    if slot_ids:
        placeholders = ",".join(["%s"] * len(slot_ids))
        cursor.execute(
            f"""
            SELECT a.slot_id, a.appointment_id, p.first_name, p.last_name,
                   p.phone_number, a.appointment_status
            FROM appointments a
            JOIN patients p ON p.patient_id = a.patient_id
            WHERE a.doctor_id = %s AND a.slot_id IN ({placeholders})
              AND a.appointment_status = 'CONFIRMED'
            ORDER BY a.appointment_id
            """,
            (doctor_id, *slot_ids),
        )
        for appointment in cursor.fetchall():
            appointments_by_slot.setdefault(int(appointment["slot_id"]), []).append(
                appointment
            )
    for item in slots:
        item["appointments"] = appointments_by_slot.get(int(item["slot_id"]), [])
        item["consultation_name"] = item.get("consultation_name", "")
    slots.sort(key=lambda item: item["start_time"])
    return response(200, {"date": local_day.isoformat(), "slots": slots})


def lambda_handler(event, context):
    try:
        return route_doctor_api(event)
    except PermissionError as error:
        return response(403, {"message": str(error)})
    except LookupError as error:
        return response(404, {"message": str(error)})
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return response(400, {"message": str(error)})
    except pymysql.err.IntegrityError:
        logger.exception("Doctor API rejected a conflicting database change")
        return response(409, {"message": "The requested change conflicts with existing data"})
    except Exception:
        logger.exception("Doctor API request failed")
        return response(500, {"message": "Internal server error"})


def weekday_name(value):
    name = str(value).upper()
    if name.isdigit():
        return ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"][int(name)]
    return name


def build_slot_id(doctor_id, consultation_id, start_time):
    identity = f"{doctor_id}|{consultation_id}|{start_time}".encode("utf-8")
    return int(hashlib.sha256(identity).hexdigest()[:15], 16)


def materialize_for_doctor_day(doctor, patterns, blackout_rows, local_day):
    created = 0
    for pattern in patterns:
        if weekday_name(pattern["day_of_week"]) != local_day.strftime("%A").upper():
            continue
        start_time = parse_sql_time(pattern["start_time"])
        end_time = parse_sql_time(pattern["end_time"])
        duration = int(pattern["duration_minutes"])
        slot_local = datetime.combine(local_day, start_time, tzinfo=timezone.utc)
        local_end = datetime.combine(local_day, end_time, tzinfo=timezone.utc)
        while slot_local + timedelta(minutes=duration) <= local_end:
            start_iso = utc_iso(slot_local)
            slot_end = slot_local + timedelta(minutes=duration)
            end_iso = utc_iso(slot_end)
            if any(
                blackout["start_datetime"].replace(tzinfo=timezone.utc) < slot_end
                and blackout["end_datetime"].replace(tzinfo=timezone.utc) > slot_local
                for blackout in blackout_rows
            ):
                slot_local += timedelta(minutes=duration)
                continue
            slot_id = build_slot_id(
                doctor["doctor_id"], pattern["consultation_type_id"], start_iso
            )
            try:
                slots_table.put_item(
                    Item={
                        "slot_id": slot_id,
                        "doctor_id": doctor["doctor_id"],
                        "consultation_type_id": pattern["consultation_type_id"],
                        "consultation_name": pattern["consultation_name"],
                        "start_time": start_iso,
                        "end_time": end_iso,
                        "local_date": local_day.isoformat(),
                        "capacity": int(pattern["capacity"]),
                        "booked_count": 0,
                        "version": 0,
                        "status": 1,
                    },
                    ConditionExpression="attribute_not_exists(slot_id)",
                )
                created += 1
            except ClientError as error:
                if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            slot_local += timedelta(minutes=duration)
    return created


def materialize_handler(event, context):
    connection = None
    try:
        window_days = int(
            get_parameter(f"/medislot/{ENVIRONMENT}/booking-window-days")
        )
        if not 1 <= window_days <= 90:
            raise ValueError("booking-window-days must be between 1 and 90")
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.doctor_id, ct.consultation_type_id,
                      ct.consultation_name, ct.duration_minutes, ct.capacity,
                      ap.day_of_week,
                       ap.start_time, ap.end_time
                FROM doctors d
                                JOIN doctor_consulting_types dct
                                    ON dct.doctor_id = d.doctor_id AND dct.is_active = TRUE
                                JOIN consultation_types ct
                                    ON ct.consultation_type_id = dct.consultation_type_id
                JOIN availability_patterns ap
                                    ON ap.consultation_type_id = ct.consultation_type_id
                                 AND ap.doctor_id = d.doctor_id
                JOIN users u ON u.user_id = d.user_id AND u.is_active = TRUE
                ORDER BY d.doctor_id, ct.consultation_type_id
                """
            )
            pattern_rows = cursor.fetchall()
            cursor.execute("SELECT doctor_id FROM doctors")
            doctors = cursor.fetchall()
            cursor.execute(
                "SELECT doctor_id, start_datetime, end_datetime FROM blackout_dates"
            )
            blackout_rows = cursor.fetchall()

        patterns_by_doctor = {}
        for pattern in pattern_rows:
            patterns_by_doctor.setdefault(pattern["doctor_id"], []).append(pattern)
        blackouts_by_doctor = {}
        for blackout in blackout_rows:
            blackouts_by_doctor.setdefault(blackout["doctor_id"], []).append(blackout)

        requested_date = event.get("date") if isinstance(event, dict) else None
        created_count = 0
        processed_dates = 0
        for doctor in doctors:
            if requested_date:
                start_day = date.fromisoformat(requested_date)
                days = 1
            else:
                start_day = datetime.now(timezone.utc).date()
                days = window_days
            patterns = patterns_by_doctor.get(doctor["doctor_id"], [])
            blackouts = blackouts_by_doctor.get(doctor["doctor_id"], [])
            for offset in range(days):
                local_day = start_day + timedelta(days=offset)
                created_count += materialize_for_doctor_day(
                    doctor, patterns, blackouts, local_day
                )
                processed_dates += 1
        logger.info(
            json.dumps(
                {
                    "event": "SlotMaterializationCompleted",
                    "processedDoctorDates": processed_dates,
                    "createdSlots": created_count,
                }
            )
        )
        return {"statusCode": 200, "created_slots": created_count, "processed_dates": processed_dates}
    except Exception:
        logger.exception("Slot materialization failed")
        raise
    finally:
        if connection:
            connection.close()