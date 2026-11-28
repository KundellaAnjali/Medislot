import html
import logging
import os
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

SENDER_EMAIL = os.environ["NOTIFICATION_SENDER_EMAIL"]
NOTIFICATIONS_TABLE = boto3.resource("dynamodb").Table(
    os.environ["EMAIL_NOTIFICATIONS_TABLE_NAME"]
)
SES = boto3.client("ses")
DELIVERY_LEASE_SECONDS = 60


def _claim_notification(notification_id, appointment_id, detail_type):
    now = int(time.time())
    lease_until = now + DELIVERY_LEASE_SECONDS
    try:
        NOTIFICATIONS_TABLE.put_item(
            Item={
                "notification_id": notification_id,
                "appointment_id": appointment_id,
                "event_type": detail_type,
                "status": "SENDING",
                "lease_until": lease_until,
                "created_at": now,
            },
            ConditionExpression="attribute_not_exists(notification_id)",
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise

    try:
        NOTIFICATIONS_TABLE.update_item(
            Key={"notification_id": notification_id},
            UpdateExpression="SET #status = :sending, lease_until = :lease",
            ConditionExpression=(
                "#status <> :sent AND "
                "(attribute_not_exists(lease_until) OR lease_until <= :now)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":sending": "SENDING",
                ":sent": "SENT",
                ":lease": lease_until,
                ":now": now,
            },
        )
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != (
            "ConditionalCheckFailedException"
        ):
            raise
        existing = NOTIFICATIONS_TABLE.get_item(
            Key={"notification_id": notification_id},
            ConsistentRead=True,
        ).get("Item")
        if existing and existing.get("status") == "SENT":
            logger.info(
                "Skipping duplicate appointment email: appointment_id=%s type=%s",
                appointment_id,
                detail_type,
            )
            return False
        raise RuntimeError(
            "Appointment email delivery is already in progress."
        ) from error


def _release_notification(notification_id):
    NOTIFICATIONS_TABLE.update_item(
        Key={"notification_id": notification_id},
        UpdateExpression="SET #status = :retry, lease_until = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":retry": "RETRY",
            ":now": int(time.time()),
        },
    )


def _email_content(detail_type, detail, patient_name, appointment_id, slot_time):
    if detail_type == "AppointmentBooked":
        subject = f"Appointment booked - #{appointment_id}"
        summary = (
            f"Hello {patient_name}, your appointment has been booked successfully."
        )
        text = (
            f"{summary}\n\nAppointment reference: #{appointment_id}\n"
            f"Appointment time: {slot_time} (IST)\n\n"
            "Please keep this reference for your records."
        )
        html_body = (
            "<p>"
            + html.escape(summary)
            + "</p><p><strong>Appointment reference:</strong> #"
            + html.escape(appointment_id)
            + "</p><p><strong>Appointment time:</strong> "
            + html.escape(slot_time)
            + " (IST)</p><p>Please keep this reference for your records.</p>"
        )
        return subject, text, html_body

    refund_amount = detail.get("refund_amount")
    refund_line = (
        f"Refund amount recorded: {refund_amount}\n"
        if refund_amount is not None
        else ""
    )
    subject = f"Appointment cancelled - #{appointment_id}"
    summary = f"Hello {patient_name}, your appointment has been cancelled."
    text = (
        f"{summary}\n\nAppointment reference: #{appointment_id}\n"
        f"Appointment time: {slot_time} (IST)\n"
        f"{refund_line}\n"
        "MediSlot records simulated payments and refunds; no money is moved "
        "by this application."
    )
    html_body = (
        "<p>"
        + html.escape(summary)
        + "</p><p><strong>Appointment reference:</strong> #"
        + html.escape(appointment_id)
        + "</p><p><strong>Appointment time:</strong> "
        + html.escape(slot_time)
        + " (IST)</p>"
    )
    if refund_amount is not None:
        html_body += (
            "<p><strong>Refund amount recorded:</strong> "
            + html.escape(str(refund_amount))
            + "</p>"
        )
    html_body += (
        "<p>MediSlot records simulated payments and refunds; no money is moved "
        "by this application.</p>"
    )
    return subject, text, html_body


def lambda_handler(event, context):
    detail_type = event.get("detail-type")
    if detail_type not in {"AppointmentBooked", "AppointmentCancelled"}:
        raise ValueError("Unsupported appointment notification event.")

    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise ValueError("Appointment notification event has no detail.")

    try:
        appointment_id = int(detail["appointment_id"])
        recipient = detail["patient_email"].strip()
        patient_name = (detail.get("patient_first_name") or "there").strip()
        slot_time = detail["slot_start_time"].strip()
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError(
            "Appointment notification event is missing required fields."
        ) from error

    if appointment_id <= 0 or not recipient or not slot_time:
        raise ValueError("Appointment notification event contains invalid fields.")

    notification_id = f"{appointment_id}:{detail_type}"
    if not _claim_notification(notification_id, appointment_id, detail_type):
        return {"status": "duplicate", "appointment_id": appointment_id}

    subject, text, html_body = _email_content(
        detail_type,
        detail,
        patient_name,
        str(appointment_id),
        slot_time,
    )
    try:
        SES.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [recipient]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        NOTIFICATIONS_TABLE.update_item(
            Key={"notification_id": notification_id},
            UpdateExpression=(
                "SET #status = :sent, sent_at = :now "
                "REMOVE lease_until"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":sent": "SENT",
                ":now": int(time.time()),
            },
        )
    except (BotoCoreError, ClientError):
        logger.exception(
            "Appointment email delivery failed: appointment_id=%s type=%s",
            appointment_id,
            detail_type,
        )
        try:
            _release_notification(notification_id)
        except (BotoCoreError, ClientError):
            logger.exception(
                "Could not release appointment email delivery claim: "
                "appointment_id=%s type=%s",
                appointment_id,
                detail_type,
            )
        raise

    logger.info(
        "Appointment email sent: appointment_id=%s type=%s",
        appointment_id,
        detail_type,
    )
    return {"status": "sent", "appointment_id": appointment_id}
