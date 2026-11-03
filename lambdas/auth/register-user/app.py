import json

import os
import re
import logging

import bcrypt
import boto3
import pymysql

# AWS Clients
ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")

# Logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment
ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

# CloudWatch Metrics
METRIC_NAMESPACE = "MediSlot/Auth"


def publish_metric(metric_name, value):
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": metric_name,
                "Value": value,
                "Unit": "Count",
            }
        ],
    )


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
        decrypt=True,
    )

    return pymysql.connect(
        host=host,
        port=port,
        user=username,
        password=password,
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def validate_email(email):

    pattern = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"

    return re.match(pattern, email)


def validate_password(password):

    if len(password) < 10:
        return False

    if not re.search(r"[A-Za-z]", password):
        return False

    if not re.search(r"\d", password):
        return False

    return True


def lambda_handler(event, context):

    connection = None

    try:

        body = json.loads(event.get("body", "{}"))

        email = body.get("email", "").strip().lower()
        password = body.get("password")
        role = body.get("role")
        first_name = body.get("first_name")
        last_name = body.get("last_name")

        logger.info(
            f"Registration started for email: {email}"
        )

        # Email Validation
        if not email:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Email is required"
                    }
                ),
            }

        if not validate_email(email):
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Invalid email format"
                    }
                ),
            }

        # Password Validation
        if not password:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Password is required"
                    }
                ),
            }

        if not validate_password(password):

            publish_metric(
                "RegistrationFailure",
                1,
            )

            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": (
                            "Password must contain at least "
                            "10 characters, one letter, and one number."
                        )
                    }
                ),
            }

        # Role Validation
        if role not in ["DOCTOR", "PATIENT"]:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Invalid role"
                    }
                ),
            }

        # Name Validation
        if not first_name:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "First name is required"
                    }
                ),
            }

        if not last_name:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Last name is required"
                    }
                ),
            }

        # Hash Password
        password_hash = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")

        connection = get_connection()

        with connection.cursor() as cursor:

            # Check Existing User
            cursor.execute(
                """
                SELECT user_id
                FROM users
                WHERE email = %s
                """,
                (email,),
            )

            existing_user = cursor.fetchone()

            if existing_user:

                publish_metric(
                    "RegistrationFailure",
                    1,
                )

                return {
                    "statusCode": 409,
                    "body": json.dumps(
                        {
                            "message": "Email already exists"
                        }
                    ),
                }


            # USERS Table
            cursor.execute(
                """
                INSERT INTO users (
                    email,
                    password_hash,
                    role,
                    is_active
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                (
                    email,
                    password_hash,
                    role,
                    True
                ),
            )

            user_id = cursor.lastrowid

            # DOCTOR Registration
            if role == "DOCTOR":

                

                cursor.execute(
                    """
                    INSERT INTO doctors (
                        user_id,
                        first_name,
                        last_name,
                        is_verified
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                    """,
                    (
                        user_id,
                        first_name,
                        last_name,
                        False
                    )
                )

            # PATIENT Registration
            else:

                

                cursor.execute(
                    """
                    INSERT INTO patients (
                        user_id,
                        first_name,
                        last_name,
                        profile_filled
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s
                    )
                    """,
                    (
                        user_id,
                        first_name,
                        last_name,
                        False
                    )
                )

        connection.commit()

        logger.info(
            f"Registration successful. User ID: {user_id}"
        )

        publish_metric(
            "RegistrationSuccess",
            1,
        )

        return {
            "statusCode": 201,
            "body": json.dumps(
                {
                    "success": True,
                    "message": "Registration successful",
                    "data": {
                        "user_id": user_id,
                        "role": role
                        
                    }
                }
            ),
        }

    except Exception as ex:

        logger.exception(
            f"Registration failed: {str(ex)}"
        )

        if connection:
            connection.rollback()

        publish_metric(
            "RegistrationFailure",
            1,
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message": "Internal server error"
                }
            ),
        }

    finally:

        if connection:
            connection.close()