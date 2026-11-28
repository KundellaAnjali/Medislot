import json
import logging
import os
import re
import uuid
from datetime import datetime

import boto3

try:
    from .milestone6_common import patient_actor, response
except ImportError:
    from milestone6_common import patient_actor, response


logger = logging.getLogger()
logger.setLevel(logging.INFO)
stepfunctions = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["RESCHEDULE_STATE_MACHINE_ARN"]
IST_SLOT_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+05:30$"
)


def lambda_handler(event, context):
    user_id = patient_actor(event)
    if user_id is None:
        return response(401, {"message": "Patient authentication is required."})
    raw_id = (event.get("pathParameters") or {}).get("appointment_id", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", str(raw_id)):
        return response(400, {"message": "Invalid appointment_id."})
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, json.JSONDecodeError):
        return response(400, {"message": "Request body must be valid JSON."})
    if (
        not isinstance(body, dict)
        or set(body) != {"slot_start_time"}
        or not isinstance(body["slot_start_time"], str)
        or not IST_SLOT_TIME_PATTERN.fullmatch(body["slot_start_time"])
    ):
        return response(
            400,
            {
                "message": (
                    "Request must contain only slot_start_time as a valid "
                    "IST ISO-8601 timestamp with +05:30 offset."
                )
            },
        )
    try:
        datetime.fromisoformat(body["slot_start_time"])
    except ValueError:
        return response(
            400,
            {"message": "slot_start_time must be a valid IST ISO-8601 timestamp."},
        )
    try:
        execution = stepfunctions.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=f"reschedule-{raw_id}-{uuid.uuid4().hex[:20]}",
            input=json.dumps(
                {
                    "user_id": user_id,
                    "appointment_id": int(raw_id),
                    "target_slot_start_time": body["slot_start_time"],
                }
            ),
        )
    except Exception:
        logger.exception(
            "Could not start reschedule execution: appointment_id=%s", raw_id
        )
        return response(503, {"message": "Could not start the reschedule workflow."})
    return response(
        202,
        {
            "success": True,
            "status": "PROCESSING",
            "execution_arn": execution["executionArn"],
        },
    )
