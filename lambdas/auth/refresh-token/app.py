import json
import uuid
import os
import logging
from datetime import datetime, timedelta, timezone

import boto3
import jwt

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


def log_event(level, correlation_id, event_name, details=None):

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


def lambda_handler(event, context):

    correlation_id = str(uuid.uuid4())

    try:

        body = json.loads(event["body"])

        refresh_token = body.get("refresh_token")

        if not refresh_token:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "message": "Refresh token is required"
                    }
                )
            }

        table = dynamodb.Table(
            f"{ENVIRONMENT}-user-sessions"
        )

        response = table.scan()

        session = None

        for item in response.get("Items", []):

            if item.get("refresh_token") == refresh_token:
                session = item
                break

        if not session:

            publish_metric(
                "RefreshFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message": "Invalid refresh token"
                    }
                )
            }

        if session["expires_at"] < int(datetime.utcnow().timestamp()):

            publish_metric(
                "RefreshFailure",
                1
            )

            return {
                "statusCode": 401,
                "body": json.dumps(
                    {
                        "message": "Refresh token expired"
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

        payload = {
            "sub": session["user_id"],
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc)
            + timedelta(
                minutes=access_expiry_minutes
            )
        }

        access_token = jwt.encode(
            payload,
            jwt_secret,
            algorithm="HS256"
        )

        log_event(
            "INFO",
            correlation_id,
            "AccessTokenRefreshed",
            {
                "userId": session["user_id"]
            }
        )

        publish_metric(
            "RefreshSuccess",
            1
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "access_token": access_token,
                    "correlation_id": correlation_id
                }
            )
        }

    except Exception as exception:

        log_event(
            "ERROR",
            correlation_id,
            "RefreshFailed",
            {
                "error": str(exception)
            }
        )

        publish_metric(
            "RefreshFailure",
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