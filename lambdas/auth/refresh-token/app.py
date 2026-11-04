import json
import os
import logging
import hashlib
from datetime import datetime, timedelta, timezone

import boto3
import jwt
import pymysql

from boto3.dynamodb.conditions import Key

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


def lambda_handler(event, context):

    connection = None

    try:

        body = json.loads(
            event.get("body", "{}")
        )

        refresh_token = body.get(
            "refresh_token"
        )

        if not refresh_token:

            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message":
                        "Refresh token is required"
                    }
                )
            }

        refresh_token_hash = hashlib.sha256(
            refresh_token.encode("utf-8")
        ).hexdigest()

        table = dynamodb.Table(
            f"{ENVIRONMENT}-user-sessions"
        )

        response = table.query(
            IndexName="RefreshTokenHashIndex",
            KeyConditionExpression=Key(
                "refresh_token_hash"
            ).eq(refresh_token_hash)
        )

        items = response.get(
            "Items",
            []
        )

        session = (
            items[0]
            if items
            else None
        )

        if not session:

            publish_metric(
                "RefreshFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message":
                        "Invalid refresh token"
                    }
                )
            }

        current_timestamp = int(
            datetime.now(
                timezone.utc
            ).timestamp()
        )

        if (
            session["expires_at"]
            < current_timestamp
        ):

            publish_metric(
                "RefreshFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message":
                        "Refresh token expired"
                    }
                )
            }

        connection = get_connection()

        with connection.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    user_id,
                    email,
                    role
                FROM users
                WHERE user_id = %s
                AND is_active = TRUE
                """,
                (
                    session["user_id"],
                )
            )

            user = cursor.fetchone()

        if not user:

            publish_metric(
                "RefreshFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message":
                        "User not found"
                    }
                )
            }

        jwt_secret = get_parameter(
            f"/medislot/{ENVIRONMENT}/jwt-secret",
            decrypt=True
        )

        access_expiry_minutes = int(
            get_parameter(
                f"/medislot/{ENVIRONMENT}/access-token-expiry"
            )
        )

        access_token_jti = session["session_id"]
        payload = {
            "sub": str(user["user_id"]),
            "email": user["email"],
            "role": user["role"],
            "jti": access_token_jti,
            "iat": datetime.now(
                timezone.utc
            ),
            "exp": datetime.now(
                timezone.utc
            ) + timedelta(
                minutes=access_expiry_minutes
            )
        }

        access_token = jwt.encode(
            payload,
            jwt_secret,
            algorithm="HS256"
        )

        logger.info(
            (
                "Access token refreshed "
                f"for user "
                f"{user['user_id']}"
            )
        )

        publish_metric(
            "RefreshSuccess",
            1
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "access_token":
                    access_token,
                    "expires_in":
                    access_expiry_minutes * 60
                }
            )
        }

    except Exception as exception:

        logger.exception(
            f"Refresh failed: {str(exception)}"
        )

        publish_metric(
            "RefreshFailure",
            1
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message":
                    "Internal server error"
                }
            )
        }

    finally:

        if connection:
            connection.close()