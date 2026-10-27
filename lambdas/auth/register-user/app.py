import json
import uuid
import os
import re
import logging
from datetime import datetime

import bcrypt
import boto3
import pymysql


ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

METRIC_NAMESPACE = "MediSlot/Auth"


def publish_metric(metric_name, value):

    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": metric_name,
                "Value": value,
                "Unit": "Count"
            }
        ]
    )


def log_event(
    level,
    correlation_id,
    event_name,
    details=None
):

    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "correlationId": correlation_id,
        "event": event_name,
        "details": details or {}
    }

    if level == "ERROR":
        logger.error(json.dumps(payload))
    else:
        logger.info(json.dumps(payload))


def get_parameter(name, decrypt=False):

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    return response["Parameter"]["Value"]


def get_connection():

    host = get_parameter(
        f"/medislot/{ENVIRONMENT}/rds-host"
    )

    port = int(
        get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-port"
        )
    )

    database = get_parameter(
        f"/medislot/{ENVIRONMENT}/rds-database"
    )

    username = get_parameter(
        f"/medislot/{ENVIRONMENT}/rds-user"
    )

    password = get_parameter(
        f"/medislot/{ENVIRONMENT}/rds-password",
        decrypt=True
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor
    )


def validate_email(email):

    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

    return re.match(pattern, email)


def validate_password(password):

    if len(password) < 8:
        return False

    if not re.search(r"[A-Z]", password):
        return False

    if not re.search(r"[a-z]", password):
        return False

    if not re.search(r"\d", password):
        return False

    if not re.search(r"[!@#$%^&*()_+\-=]", password):
        return False

    return True


def lambda_handler(event, context):

    correlation_id = str(uuid.uuid4())

    try:

        body = json.loads(event["body"])

        email = body.get("email")
        password = body.get("password")
        role = body.get("role")
        full_name = body.get("full_name")

        if email:
            email = email.strip().lower()

        log_event(
            "INFO",
            correlation_id,
            "UserRegistrationStarted",
            {
                "email": email,
                "role": role
            }
        )

        if not email:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Email is required"
                    }
                )
            }

        if not validate_email(email):
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Invalid email format"
                    }
                )
            }

        if not full_name:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Full name is required"
                    }
                )
            }

        if not password:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Password is required"
                    }
                )
            }

        if role not in ["DOCTOR", "PATIENT"]:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Invalid role"
                    }
                )
            }

        if not validate_password(password):

            log_event(
                "ERROR",
                correlation_id,
                "PasswordValidationFailed",
                {
                    "email": email
                }
            )

            publish_metric(
                "RegistrationFailure",
                1
            )

            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Password policy validation failed"
                    }
                )
            }

        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=12)
        ).decode("utf-8")

        connection = get_connection()

        try:

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT email
                    FROM users
                    WHERE email = %s
                    """,
                    (email,)
                )

                existing_user = cursor.fetchone()

                if existing_user:

                    log_event(
                        "ERROR",
                        correlation_id,
                        "DuplicateEmailRegistration",
                        {
                            "email": email
                        }
                    )

                    publish_metric(
                        "RegistrationFailure",
                        1
                    )

                    return {
                        "statusCode": 409,
                        "body": json.dumps(
                            {
                                "message": "Email already exists"
                            }
                        )
                    }

                user_id = str(uuid.uuid4())

                cursor.execute(
                    """
                    INSERT INTO users (
                        user_id,
                        full_name,
                        email,
                        password_hash,
                        role,
                        is_active
                    )
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        user_id,
                        full_name,
                        email,
                        password_hash,
                        role,
                        True
                    )
                )

                if role == "DOCTOR":

                    doctor_id = str(uuid.uuid4())

                    cursor.execute(
                        """
                        INSERT INTO doctors (
                            doctor_id,
                            user_id,
                            full_name,
                            is_verified
                        )
                        VALUES (%s,%s,%s,%s)
                        """,
                        (
                            doctor_id,
                            user_id,
                            full_name,
                            False
                        )
                    )

                else:

                    patient_id = str(uuid.uuid4())

                    cursor.execute(
                        """
                        INSERT INTO patients (
                            patient_id,
                            user_id,
                            full_name
                        )
                        VALUES (%s,%s,%s)
                        """,
                        (
                            patient_id,
                            user_id,
                            full_name
                        )
                    )

            connection.commit()

        except Exception:

            connection.rollback()
            raise

        finally:
            connection.close()

        log_event(
            "INFO",
            correlation_id,
            "UserRegistrationCompleted",
            {
                "userId": user_id,
                "role": role
            }
        )

        publish_metric(
            "RegistrationSuccess",
            1
        )

        return {
            "statusCode": 201,
            "body": json.dumps(
                {
                    "message": "User registered successfully",
                    "user_id": user_id,
                    "correlation_id": correlation_id
                }
            )
        }

    except Exception as exception:

        log_event(
            "ERROR",
            correlation_id,
            "UserRegistrationFailed",
            {
                "error": str(exception)
            }
        )

        publish_metric(
            "RegistrationFailure",
            1
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message": "Internal server error",
                    "correlation_id": correlation_id
                }
            )
        }