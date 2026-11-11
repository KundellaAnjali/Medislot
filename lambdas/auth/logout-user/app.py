import json
import os
import logging
import hashlib

import boto3
from boto3.dynamodb.conditions import Key

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


def lambda_handler(event, context):

    request_id = getattr(context, "aws_request_id", "unknown")

    try:

        body = json.loads(
            event.get("body", "{}")
        )

        refresh_token = body.get(
            "refresh_token"
        )

        if not refresh_token:
            logger.warning(
                "Logout rejected: missing refresh token request_id=%s",
                request_id,
            )

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
            logger.warning(
                "Logout rejected: session not found request_id=%s",
                request_id,
            )

            publish_metric(
                "LogoutFailure",
                1
            )

            return {
                "statusCode": 404,
                "body": json.dumps(
                    {
                        "message":
                        "Session not found"
                    }
                )
            }

        table.delete_item(
            Key={
                "session_id":
                session["session_id"]
            }
        )

        logger.info(
            "Logout successful: user_id=%s request_id=%s",
            session["user_id"],
            request_id,
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
                    "Logout successful"
                }
            )
        }

    except Exception as exception:

        logger.exception(
            "Logout failed: request_id=%s",
            request_id,
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
                    "Internal server error"
                }
            )
        }