import json
import uuid
import os
import logging
from datetime import datetime

import boto3

dynamodb = boto3.resource("dynamodb")
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


def lambda_handler(event, context):

    correlation_id = str(uuid.uuid4())

    try:

        body = json.loads(event["body"])

        refresh_token = body.get(
            "refresh_token"
        )

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

            if (
                item.get("refresh_token")
                == refresh_token
            ):
                session = item
                break

        if not session:

            publish_metric(
                "LogoutFailure",
                1
            )

            return {
                "statusCode": 404,
                "body": json.dumps(
                    {
                        "message": "Session not found"
                    }
                )
            }

        table.delete_item(
            Key={
                "session_id":
                session["session_id"]
            }
        )

        log_event(
            "INFO",
            correlation_id,
            "UserLogoutCompleted",
            {
                "userId":
                session["user_id"]
            }
        )

        publish_metric(
            "LogoutSuccess",
            1
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message":
                    "Logout successful",
                    "correlation_id":
                    correlation_id
                }
            )
        }

    except Exception as exception:

        log_event(
            "ERROR",
            correlation_id,
            "UserLogoutFailed",
            {
                "error":
                str(exception)
            }
        )

        publish_metric(
            "LogoutFailure",
            1
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message":
                    "Internal server error",
                    "correlation_id":
                    correlation_id
                }
            )
        }