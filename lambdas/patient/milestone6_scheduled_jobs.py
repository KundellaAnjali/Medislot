import logging
from datetime import timedelta, timezone

import boto3

try:
    from .appointment_repository import get_connection
    from .milestone6_common import (
        get_parameter,
        india_now,
        publish_event,
        publish_metric,
    )
    from .refund_service import RefundService
except ImportError:
    from appointment_repository import get_connection
    from milestone6_common import (
        get_parameter,
        india_now,
        publish_event,
        publish_metric,
    )
    from refund_service import RefundService


logger = logging.getLogger()
logger.setLevel(logging.INFO)
cloudwatch = boto3.client("cloudwatch")
INDIA_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


def india_iso(value):
    return value.replace(tzinfo=INDIA_TIMEZONE).isoformat(timespec="seconds")


def reminder_lambda_handler(event, context):
    reminder_windows = (
        get_parameter("REMINDER_24_HOURS_BEFORE"),
        get_parameter("REMINDER_2_HOURS_BEFORE"),
    )
    interval = get_parameter("REMINDER_SWEEP_INTERVAL_MINUTES")
    connection = get_connection()
    events = []
    now = india_now().replace(tzinfo=None)
    try:
        with connection.cursor() as cursor:
            for hours in reminder_windows:
                cursor.execute(
                    """
                    SELECT a.appointment_id, a.patient_id, a.doctor_id,
                           a.slot_id, a.slot_start_time
                    FROM appointments a
                    WHERE a.status = 'BOOKED'
                      AND a.appointment_status = 'CONFIRMED'
                      AND a.slot_start_time > %s
                      AND a.slot_start_time <= DATE_ADD(
                          %s, INTERVAL %s HOUR
                      )
                      AND a.slot_start_time > DATE_SUB(
                          DATE_ADD(%s, INTERVAL %s HOUR),
                          INTERVAL %s MINUTE
                      )
                    """,
                    (now, now, hours, now, hours, interval),
                )
                events.extend(
                    {
                        "appointment_id": int(row["appointment_id"]),
                        "patient_id": int(row["patient_id"]),
                        "doctor_id": int(row["doctor_id"]),
                        "slot_id": int(row["slot_id"]),
                        "slot_start_time": india_iso(row["slot_start_time"]),
                        "reminder_hours": hours,
                    }
                    for row in cursor.fetchall()
                )
    finally:
        connection.close()

    dispatched = 0
    for reminder in events:
        reminder["notification_id"] = (
            f"appointment-reminder-{reminder['appointment_id']}-"
            f"{reminder['reminder_hours']}h"
        )
        publish_event("AppointmentReminder", reminder)
        dispatched += 1
    return {"success": True, "reminders_published": dispatched}


def no_show_lambda_handler(event, context):
    grace_minutes = get_parameter("NO_SHOW_GRACE_MINUTES")
    connection = get_connection()
    due = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id,
                       a.appointment_datetime, a.slot_start_time,
                       ct.duration_minutes
                FROM appointments a
                JOIN consultation_types ct
                  ON ct.consultation_type_id = a.consultation_type_id
                WHERE a.status = 'BOOKED'
                  AND a.appointment_status = 'CONFIRMED'
                  AND DATE_ADD(
                      DATE_ADD(
                          COALESCE(a.slot_start_time, a.appointment_datetime),
                          INTERVAL ct.duration_minutes MINUTE
                      ),
                      INTERVAL %s MINUTE
                  ) <= %s
                ORDER BY a.appointment_id
                """,
                (
                    grace_minutes,
                    india_now().replace(tzinfo=None),
                ),
            )
            due = cursor.fetchall()
    finally:
        connection.close()

    changed = []
    refunds = RefundService()
    for appointment in due:
        result = mark_stale_no_show(appointment, refunds, grace_minutes)
        if result:
            changed.append(result)
    for appointment in changed:
        publish_event(
            "AppointmentNoShow",
            {
                "notification_id": f"appointment-no-show-{appointment['appointment_id']}",
                **appointment,
            },
        )
        publish_event(
            "RefundRecorded",
            {
                "notification_id": (
                    f"refund-recorded-no-show-{appointment['appointment_id']}"
                ),
                "appointment_id": appointment["appointment_id"],
                "patient_id": appointment["patient_id"],
                "doctor_id": appointment["doctor_id"],
                "refund_amount": appointment["refund_amount"],
                "refund_reason": appointment["refund_reason"],
                "status": "NOT_REQUIRED",
            },
        )
    return {"success": True, "no_show_count": len(changed)}


def mark_stale_no_show(appointment, refund_service, grace_minutes):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.patient_id, a.doctor_id, a.slot_id,
                       COALESCE(a.slot_start_time, a.appointment_datetime)
                           AS appointment_datetime
                FROM appointments a
                WHERE a.appointment_id = %s AND a.status = 'BOOKED'
                  AND a.appointment_status = 'CONFIRMED'
                FOR UPDATE
                """,
                (appointment["appointment_id"],),
            )
            row = cursor.fetchone()
            if not row:
                connection.rollback()
                return None
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS paid_amount
                FROM payments
                WHERE appointment_id = %s
                  AND (status IN ('PAID', 'COMPLETED', 'SUCCESS', 'REFUNDED')
                       OR (status = 'SIMULATED'
                           AND payment_method = 'SIMULATED'))
                """,
                (appointment["appointment_id"],),
            )
            row["paid_amount"] = cursor.fetchone()["paid_amount"]
            now = india_now()
            refund = refund_service.calculate(
                row["appointment_datetime"],
                now,
                row["paid_amount"],
                no_show=True,
            )
            cursor.execute(
                """
                UPDATE appointments
                SET status = 'NO_SHOW', appointment_status = 'NO_SHOW'
                WHERE appointment_id = %s AND status = 'BOOKED'
                  AND appointment_status = 'CONFIRMED'
                """,
                (appointment["appointment_id"],),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            insert_refund(cursor, appointment, row, refund)
        connection.commit()
        publish_metric("NoShowAppointments")
        return {
            "appointment_id": str(appointment["appointment_id"]),
            "patient_id": str(row["patient_id"]),
            "doctor_id": str(row["doctor_id"]),
            "slot_id": str(row["slot_id"]),
            "slot_start_time": india_iso(appointment["slot_start_time"]),
            "status": "NO_SHOW",
            "refund_amount": str(refund["refund_amount"]),
            "refund_reason": refund["refund_reason"],
            "grace_minutes": grace_minutes,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_refund(cursor, appointment, row, refund):
    values = (
        appointment["appointment_id"],
        row["patient_id"],
        refund["refund_amount"],
        refund["deduction_amount"],
        refund["refund_percentage"],
        refund["refund_reason"],
    )
    cursor.execute(
        """
        INSERT INTO refunds
            (appointment_id, patient_id, refund_amount, deduction_amount,
             refund_percentage, refund_transaction_id, refund_reason, status,
             created_at, refunded_at)
        VALUES (%s, %s, %s, %s, %s, NULL, %s, 'NOT_REQUIRED',
                UTC_TIMESTAMP(6), NULL)
        """,
        values,
    )
