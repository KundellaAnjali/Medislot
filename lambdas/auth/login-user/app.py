import json
import uuid
import os
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import boto3
import jwt
import pymysql


ssm = boto3.client("ssm")
dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

METRIC_NAMESPACE = "MediSlot/Auth"

parameter_cache = {}


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

    if name in parameter_cache:
        return parameter_cache[name]

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    value = response["Parameter"]["Value"]

    parameter_cache[name] = value

    return value


def get_connection():

    return pymysql.connect(
        host=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-host"
        ),
        port=int(
            get_parameter(
                f"/medislot/{ENVIRONMENT}/rds-port"
            )
        ),
        user=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-user"
        ),
        password=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-password",
            decrypt=True
        ),
        database=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-database"
        ),
        cursorclass=pymysql.cursors.DictCursor
    )


def create_access_token(user):

    jwt_secret = get_parameter(
        f"/medislot/{ENVIRONMENT}/jwt-secret",
        decrypt=True
    )

    access_expiry_minutes = int(
        get_parameter(
            f"/medislot/{ENVIRONMENT}/access-token-expiry"
        )
    )

    payload = {
        "sub": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=access_expiry_minutes)
    }

    return jwt.encode(
        payload,
        jwt_secret,
        algorithm="HS256"
    )


def create_refresh_token():

    return str(uuid.uuid4())


def save_session(
    user_id,
    refresh_token
):

    table = dynamodb.Table(
        f"{ENVIRONMENT}-user-sessions"
    )

    refresh_expiry_days = int(
        get_parameter(
            f"/medislot/{ENVIRONMENT}/refresh-token-expiry"
        )
    )

    expiry = int(
        (
            datetime.utcnow()
            + timedelta(days=refresh_expiry_days)
        ).timestamp()
    )

    table.put_item(
        Item={
            "session_id": str(uuid.uuid4()),
            "user_id": user_id,
            "refresh_token": refresh_token,
            "expires_at": expiry
        }
    )


def lambda_handler(event, context):

    correlation_id = str(uuid.uuid4())

    try:

        body = json.loads(event["body"])

        email = body.get("email")
        password = body.get("password")

        if email:
            email = email.strip().lower()

        log_event(
            "INFO",
            correlation_id,
            "UserLoginStarted",
            {
                "email": email
            }
        )

        if not email or not password:

            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Email and password are required"
                    }
                )
            }

        connection = get_connection()

        try:

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        user_id,
                        email,
                        password_hash,
                        role
                    FROM users
                    WHERE email=%s
                    """,
                    (email,)
                )

                user = cursor.fetchone()

        finally:
            connection.close()

        if not user:

            publish_metric(
                "LoginFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message": "Invalid credentials"
                    }
                )
            }

        password_matches = bcrypt.checkpw(
            password.encode("utf-8"),
            user["password_hash"].encode("utf-8")
        )

        if not password_matches:

            publish_metric(
                "LoginFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message": "Invalid credentials"
                    }
                )
            }

        access_token = create_access_token(
            user
        )

        refresh_token = create_refresh_token()

        save_session(
            user["user_id"],
            refresh_token
        )

        log_event(
            "INFO",
            correlation_id,
            "UserLoginCompleted",
            {
                "userId": user["user_id"]
            }
        )

        publish_metric(
            "LoginSuccess",
            1
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "correlation_id": correlation_id
                }
            )
        }

    except Exception as exception:

        log_event(
            "ERROR",
            correlation_id,
            "UserLoginFailed",
            {
                "error": str(exception)
            }
        )

        publish_metric(
            "LoginFailure",
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