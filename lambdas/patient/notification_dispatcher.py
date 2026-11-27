import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

try:
    from .milestone6_common import get_parameter
except ImportError:
    from milestone6_common import get_parameter


logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")
cloudwatch = boto3.client("cloudwatch")
dispatch_log = dynamodb.Table(os.environ["IDEMPOTENCY_TABLE_NAME"])
topic_arn = os.environ["NOTIFICATION_TOPIC_ARN"]


def lambda_handler(event, context):
    dispatched = 0
    for record in event.get("Records", []):
        envelope = json.loads(record["body"])
        detail_type = envelope.get("detail-type", "UnknownEvent")
        detail = envelope.get("detail") or {}
        notification_id = str(
            detail.get("notification_id")
            or f"{envelope.get('id', 'unknown')}-{detail_type}"
        )
        try:
            dispatch_one(notification_id, detail_type, detail, envelope)
        except Exception:
            logger.exception(
                "Notification dispatch failed: notification_id=%s event=%s",
                notification_id,
                detail_type,
            )
            raise
        dispatched += 1
    return {"success": True, "dispatched": dispatched}


def dispatch_one(notification_id, detail_type, detail, envelope):
    now = int(time.time())
    item = {
        "idempotency_key": notification_id,
        "notification_id": notification_id,
        "status": "DISPATCHING",
        "detail_type": detail_type,
        "created_at": now,
        "expires_at": now + get_parameter("NOTIFICATION_DISPATCH_TTL_DAYS") * 24 * 60 * 60,
    }
    try:
        dispatch_log.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(idempotency_key)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("Skipping duplicate notification: id=%s", notification_id)
            return False
        raise

    message = {
        "notification_id": notification_id,
        "notification_type": detail_type,
        "occurred_at": envelope.get("time"),
        "correlation_id": detail.get("correlation_id"),
        "detail": detail,
    }
    try:
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"MediSlot {detail_type}",
            Message=json.dumps(message, default=str, separators=(",", ":")),
            MessageAttributes={
                "notification_type": {
                    "DataType": "String",
                    "StringValue": detail_type,
                }
            },
        )
    except Exception:
        try:
            dispatch_log.delete_item(
                Key={"idempotency_key": notification_id},
                ConditionExpression="#status = :dispatching",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":dispatching": "DISPATCHING"},
            )
        except Exception:
            logger.exception(
                "Could not release notification dispatch claim: id=%s",
                notification_id,
            )
        raise

    dispatch_log.update_item(
        Key={"idempotency_key": notification_id},
            UpdateExpression="SET #status = :sent, sent_at = :sent_at",
            ConditionExpression="#status = :dispatching",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":sent": "SENT",
                ":sent_at": int(time.time()),
                ":dispatching": "DISPATCHING",
            },
        )
    try:
        cloudwatch.put_metric_data(
            Namespace="MediSlot/Patient",
            MetricData=[
                {
                    "MetricName": "NotificationsDispatched",
                    "Value": 1,
                    "Unit": "Count",
                }
            ],
        )
    except Exception:
        logger.exception("Could not publish notification metric: id=%s", notification_id)
    return True
