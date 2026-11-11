import json
import uuid
import os
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import boto3
import jwt
import pymysql
import hashlib

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


def create_access_token(user, token_jti):

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
        "sub": str(user["user_id"]),
        "email": user["email"],
        "role": user["role"],
        "jti": token_jti,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(minutes=access_expiry_minutes)
    }

    token = jwt.encode(
        payload,
        jwt_secret,
        algorithm="HS256"
    )

    return token


def create_refresh_token():

    return str(uuid.uuid4())


def save_session(
    user_id,
    role,
    refresh_token,
    session_id,
    token_jti,
):
    refresh_token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()

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
            datetime.now(timezone.utc)
            + timedelta(days=refresh_expiry_days)
        ).timestamp()
    )

    table.put_item(
        Item={
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "jti": token_jti,
            "refresh_token_hash": refresh_token_hash,
            "expires_at": expiry
        }
    )


def lambda_handler(event, context):

    request_id = getattr(context, "aws_request_id", "unknown")

    try:

        body = json.loads(event.get("body", "{}"))

        email = body.get("email")
        password = body.get("password")

        if email:
            email = email.strip().lower()

        logger.info("Login started")

        if not email or not password:
            logger.warning(
                "Login rejected: missing email or password request_id=%s",
                request_id,
            )

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
                        role,
                        is_active
                    FROM users
                    WHERE email = %s
                    AND is_active = TRUE
                    """,
                    (email,)
                )

                user = cursor.fetchone()

        finally:
            connection.close()

        if not user:
            logger.warning(
                "Login rejected: invalid credentials request_id=%s",
                request_id,
            )

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

        if not bcrypt.checkpw(
            password.encode("utf-8"),
            user["password_hash"].encode("utf-8")
        ):
            logger.warning(
                "Login rejected: invalid credentials request_id=%s",
                request_id,
            )

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
        
        connection = get_connection()

        try:

            with connection.cursor() as cursor:

                cursor.execute(
                    """
                    UPDATE users
                    SET last_login_at = NOW()
                    WHERE user_id = %s
                    """,
                    (user["user_id"],)
                )

            connection.commit()

        finally:
            connection.close()

        session_id = str(uuid.uuid4())
        token_jti = str(uuid.uuid4())

        access_token = create_access_token(
            user,
            token_jti
        )

        refresh_token = create_refresh_token()

        save_session(
            user["user_id"],
            user["role"],
            refresh_token,
            session_id,
            token_jti,
        )

        publish_metric(
            "LoginSuccess",
            1
        )

        logger.info(
            "Login successful: user_id=%s request_id=%s",
            user["user_id"],
            request_id,
        )

        access_expiry_minutes = int(
            get_parameter(
                f"/medislot/{ENVIRONMENT}/access-token-expiry"
            )
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": access_expiry_minutes * 60,
                    "user_id": user["user_id"],
                    "role": user["role"]
                }
            )
        }

    except Exception as exception:

        logger.exception(
            "Login failed: request_id=%s",
            request_id,
        )

        publish_metric(
            "LoginFailure",
            1
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message": "Internal server error"
                }
            )
        }