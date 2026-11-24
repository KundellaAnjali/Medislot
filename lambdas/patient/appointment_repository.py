import logging
import os
from datetime import datetime, timezone

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
ssm = boto3.client("ssm")
parameter_cache = {}


class AppointmentRepositoryError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class AppointmentRepository:
    def __init__(self, connection_factory=None):
        self.connection_factory = connection_factory or get_connection

    def create_booking(
        self,
        user_id,
        doctor_id,
        slot_start_time,
        slot_id,
        consultation_type_id,
        reserve_slot,
        release_slot,
    ):
        connection = self.connection_factory()
        reserved_slot = None
        commit_started = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT patient_id FROM patients WHERE user_id = %s FOR UPDATE",
                    (user_id,),
                )
                patient = cursor.fetchone()
                if not patient:
                    raise AppointmentRepositoryError(
                        404, "Patient profile was not found."
                    )
                patient_id = int(patient["patient_id"])
                cursor.execute(
                    "SELECT doctor_id FROM doctors WHERE doctor_id = %s FOR UPDATE",
                    (doctor_id,),
                )
                if not cursor.fetchone():
                    raise AppointmentRepositoryError(404, "Doctor was not found.")

                cursor.execute(
                    """
                    SELECT 1 FROM appointments
                    WHERE patient_id = %s AND doctor_id = %s AND slot_id = %s
                      AND (status = 'BOOKED' OR appointment_status = 'CONFIRMED')
                    LIMIT 1
                    """,
                    (patient_id, doctor_id, slot_id),
                )
                if cursor.fetchone():
                    raise AppointmentRepositoryError(
                        409,
                        "You already have an active appointment for this slot.",
                    )

                cursor.execute(
                    """
                    SELECT COUNT(*) AS active_count
                    FROM appointments
                    WHERE patient_id = %s
                      AND (status = 'BOOKED' OR appointment_status = 'CONFIRMED')
                    """,
                    (patient_id,),
                )
                active_count = int(cursor.fetchone()["active_count"])
                max_active_bookings = int(
                    get_parameter(
                        f"/medislot/{ENVIRONMENT}/patient-booking/max-active-bookings"
                    )
                )
                if max_active_bookings < 1:
                    raise ValueError(
                        "Maximum active booking limit must be a positive integer."
                    )
                if active_count >= max_active_bookings:
                    raise AppointmentRepositoryError(
                        409,
                        f"You can have up to {max_active_bookings} active appointments.",
                    )

                cursor.execute(
                    """
                    SELECT fee
                    FROM doctor_consulting_types
                    WHERE doctor_id = %s
                      AND consultation_type_id = %s
                      AND is_active = TRUE
                    FOR UPDATE
                    """,
                    (doctor_id, consultation_type_id),
                )
                consultation = cursor.fetchone()
                if not consultation:
                    raise AppointmentRepositoryError(
                        409,
                        "The consultation price is unavailable. Please choose another slot.",
                    )

                reserved_slot = reserve_slot()
                appointment_start = datetime.fromisoformat(
                    slot_start_time.replace("Z", "+00:00")
                ).replace(tzinfo=None)

                cursor.execute(
                    """
                    INSERT INTO appointments
                        (patient_id, doctor_id, slot_id, consultation_type_id,
                         appointment_status, appointment_datetime,
                         slot_start_time, status, created_at)
                    VALUES (%s, %s, %s, %s, 'CONFIRMED', %s, %s, 'BOOKED',
                            UTC_TIMESTAMP(6))
                    """,
                    (
                        patient_id,
                        doctor_id,
                        slot_id,
                        consultation_type_id,
                        appointment_start,
                        appointment_start,
                    ),
                )
                appointment_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO payments
                        (appointment_id, amount, transaction_id,
                         payment_method, status, created_at, paid_at)
                    VALUES (%s, %s, %s, 'SIMULATED', 'SIMULATED',
                            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    """,
                    (
                        appointment_id,
                        consultation["fee"],
                        f"SIMULATED-{appointment_id}",
                    ),
                )
                response_payload = {
                    "appointment_id": appointment_id,
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "slot_id": slot_id,
                    "slot_start_time": slot_start_time,
                    "status": "BOOKED",
                    "appointment_status": "CONFIRMED",
                    "booked_count": int(reserved_slot["booked_count"]),
                }

            commit_started = True
            connection.commit()
            return response_payload, True
        except Exception:
            try:
                connection.rollback()
            except Exception:
                logger.exception("Could not roll back appointment transaction")

            if reserved_slot is not None and not commit_started:
                release_slot(reserved_slot)
            elif reserved_slot is not None:
                logger.error(
                    "Slot reservation retained because appointment commit outcome "
                    "could not be determined."
                )
            raise
        finally:
            connection.close()

    def cancel_appointments(
        self,
        role,
        user_id,
        appointment_id,
        refund_config,
        refund_calculator,
        now,
        deactivate_slot=None,
    ):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT appointment_id, patient_id, doctor_id, slot_id,
                           COALESCE(slot_start_time, appointment_datetime)
                               AS slot_start_time,
                           COALESCE(status, 'BOOKED') AS status
                    FROM appointments
                    WHERE appointment_id = %s
                    FOR UPDATE
                    """,
                    (appointment_id,),
                )
                selected = cursor.fetchone()
                if not selected:
                    raise AppointmentRepositoryError(
                        404, "Appointment was not found."
                    )

                if role == "PATIENT":
                    cursor.execute(
                        "SELECT patient_id FROM patients WHERE user_id = %s",
                        (user_id,),
                    )
                    actor = cursor.fetchone()
                    if not actor:
                        raise AppointmentRepositoryError(
                            404, "Patient profile was not found."
                        )
                    if int(actor["patient_id"]) != int(selected["patient_id"]):
                        raise AppointmentRepositoryError(
                            404, "Appointment was not found."
                        )
                    if selected["status"] == "CANCELLED":
                        targets = [selected]
                        doctor_cancel = False
                    elif selected["status"] != "BOOKED":
                        raise AppointmentRepositoryError(
                            409, "Appointment is not in a cancellable state."
                        )
                    else:
                        targets = [selected]
                        doctor_cancel = False
                elif role == "DOCTOR":
                    cursor.execute(
                        "SELECT doctor_id FROM doctors WHERE user_id = %s",
                        (user_id,),
                    )
                    actor = cursor.fetchone()
                    if not actor:
                        raise AppointmentRepositoryError(
                            404, "Doctor profile was not found."
                        )
                    if int(actor["doctor_id"]) != int(selected["doctor_id"]):
                        raise AppointmentRepositoryError(
                            404, "Appointment was not found."
                        )
                    cursor.execute(
                        "SELECT doctor_id FROM doctors WHERE doctor_id = %s FOR UPDATE",
                        (selected["doctor_id"],),
                    )
                    if not cursor.fetchone():
                        raise AppointmentRepositoryError(
                            404, "Doctor was not found."
                        )
                    if deactivate_slot:
                        deactivate_slot(
                            int(selected["doctor_id"]),
                            selected["slot_start_time"],
                        )
                    cursor.execute(
                        """
                        SELECT appointment_id, patient_id, doctor_id, slot_id,
                               COALESCE(slot_start_time, appointment_datetime)
                                   AS slot_start_time,
                               COALESCE(status, 'BOOKED') AS status
                        FROM appointments
                        WHERE doctor_id = %s
                          AND slot_id = %s
                          AND COALESCE(slot_start_time, appointment_datetime) = %s
                          AND (status = 'BOOKED'
                               OR appointment_status = 'CONFIRMED')
                        ORDER BY appointment_id
                        FOR UPDATE
                        """,
                        (
                            selected["doctor_id"],
                            selected["slot_id"],
                            selected["slot_start_time"],
                        ),
                    )
                    targets = cursor.fetchall()
                    if not targets and selected["status"] == "CANCELLED":
                        cursor.execute(
                            """
                            SELECT appointment_id, patient_id, doctor_id, slot_id,
                                   COALESCE(slot_start_time, appointment_datetime)
                                       AS slot_start_time,
                                   COALESCE(status, 'BOOKED') AS status
                            FROM appointments
                            WHERE doctor_id = %s
                              AND slot_id = %s
                              AND COALESCE(slot_start_time, appointment_datetime) = %s
                              AND status = 'CANCELLED'
                            ORDER BY appointment_id
                            FOR UPDATE
                            """,
                            (
                                selected["doctor_id"],
                                selected["slot_id"],
                                selected["slot_start_time"],
                            ),
                        )
                        targets = cursor.fetchall()
                    doctor_cancel = True
                else:
                    raise AppointmentRepositoryError(403, "Role is not allowed.")

                if not targets:
                    connection.rollback()
                    return {
                        "message": "Appointment was already cancelled.",
                        "cancelled_appointments": [],
                        "already_cancelled": True,
                    }

                cancelled = []
                tier_counts = {}
                changed_any = False
                for appointment in targets:
                    if appointment["status"] == "CANCELLED":
                        cursor.execute(
                            """
                            SELECT refund_amount, refund_percentage, refund_reason
                            FROM refunds
                            WHERE appointment_id = %s
                            """,
                            (appointment["appointment_id"],),
                        )
                        existing_refund = cursor.fetchone()
                        if not existing_refund:
                            continue
                        appointment_time = appointment["slot_start_time"]
                        if appointment_time.tzinfo is None:
                            appointment_time = appointment_time.replace(
                                tzinfo=timezone.utc
                            )
                        slot_start_time = appointment_time.astimezone(
                            timezone.utc
                        ).isoformat(
                            timespec=(
                                "microseconds"
                                if appointment_time.microsecond
                                else "seconds"
                            )
                        ).replace("+00:00", "Z")
                        percentage = existing_refund["refund_percentage"]
                        tier = (
                            "FULL"
                            if percentage == 100
                            else "NONE"
                            if percentage == 0
                            else "PARTIAL"
                        )
                        cancelled.append(
                            {
                                "appointment_id": str(
                                    appointment["appointment_id"]
                                ),
                                "patient_id": str(appointment["patient_id"]),
                                "doctor_id": str(appointment["doctor_id"]),
                                "slot_id": str(appointment["slot_id"]),
                                "status": "CANCELLED",
                                "slot_start_time": slot_start_time,
                                "refund_amount": str(
                                    existing_refund["refund_amount"]
                                ),
                                "refund_percentage": str(percentage),
                                "refund_reason": existing_refund["refund_reason"],
                                "refund_tier": tier,
                            }
                        )
                        continue
                    if appointment["status"] != "BOOKED":
                        continue

                    cursor.execute(
                        """
                        SELECT COALESCE(SUM(amount), 0) AS paid_amount
                        FROM payments
                        WHERE appointment_id = %s AND
                            (status IN ('PAID', 'COMPLETED', 'SUCCESS', 'REFUNDED')
                             OR (status = 'SIMULATED'
                                 AND payment_method = 'SIMULATED'))
                        """,
                        (appointment["appointment_id"],),
                    )
                    paid_amount = cursor.fetchone()["paid_amount"]
                    appointment_time = appointment["slot_start_time"]
                    if appointment_time.tzinfo is None:
                        appointment_time = appointment_time.replace(
                            tzinfo=now.tzinfo
                        )
                    refund = refund_calculator(
                        appointment_time,
                        paid_amount,
                        refund_config,
                        doctor_cancel=doctor_cancel,
                        now=now,
                    )
                    cursor.execute(
                        """
                        UPDATE appointments
                        SET status = 'CANCELLED', appointment_status = 'CANCELLED'
                        WHERE appointment_id = %s AND status = 'BOOKED'
                        """,
                        (appointment["appointment_id"],),
                    )
                    if cursor.rowcount != 1:
                        raise AppointmentRepositoryError(
                            409, "Appointment changed while cancellation was processing."
                        )
                    changed_any = True
                    cursor.execute(
                        """
                        INSERT INTO refunds
                            (appointment_id, patient_id, refund_amount,
                             refund_percentage, refund_transaction_id,
                             refund_reason, status, created_at)
                        VALUES (%s, %s, %s, %s, NULL, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            appointment["appointment_id"],
                            appointment["patient_id"],
                            refund["refund_amount"],
                            refund["refund_percentage"],
                            refund["refund_reason"],
                            (
                                "PENDING"
                                if refund["refund_amount"] > 0
                                else "NOT_REQUIRED"
                            ),
                        ),
                    )
                    slot_start_utc = appointment_time.replace(
                        tzinfo=timezone.utc
                    )
                    time_precision = (
                        "microseconds"
                        if slot_start_utc.microsecond
                        else "seconds"
                    )
                    slot_start_time = slot_start_utc.isoformat(
                        timespec=time_precision
                    ).replace("+00:00", "Z")
                    cancellation_event = {
                        "appointment_id": str(appointment["appointment_id"]),
                        "patient_id": str(appointment["patient_id"]),
                        "doctor_id": str(appointment["doctor_id"]),
                        "slot_id": str(appointment["slot_id"]),
                        "status": "CANCELLED",
                        "slot_start_time": slot_start_time,
                        "refund_amount": str(refund["refund_amount"]),
                        "refund_percentage": str(refund["refund_percentage"]),
                        "refund_reason": refund["refund_reason"],
                    }
                    tier = refund["refund_tier"]
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1
                    cancelled.append(
                        {
                            **cancellation_event,
                            "refund_tier": tier,
                        }
                    )

            if not cancelled:
                connection.rollback()
                return {
                    "message": "Appointment was already cancelled.",
                    "cancelled_appointments": [],
                    "already_cancelled": True,
                }
            connection.commit()
            already_cancelled = not changed_any
            return {
                "message": (
                    "Appointment was already cancelled."
                    if already_cancelled
                    else "Appointment cancellation accepted."
                ),
                "cancelled_appointments": cancelled,
                "already_cancelled": already_cancelled,
                "tier_counts": [
                    {"tier": tier, "count": count}
                    for tier, count in sorted(tier_counts.items())
                ],
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_patient_refunds(self, user_id, limit=50, offset=0):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT patient_id FROM patients WHERE user_id = %s",
                    (user_id,),
                )
                patient = cursor.fetchone()
                if not patient:
                    raise AppointmentRepositoryError(
                        404, "Patient profile was not found."
                    )
                cursor.execute(
                    """
                    SELECT refund_id, appointment_id, patient_id, refund_amount,
                           refund_percentage, refund_transaction_id,
                           refund_reason, status, created_at, refunded_at,
                           CASE
                               WHEN refund_percentage = 100 THEN 'FULL'
                               WHEN refund_percentage = 0 THEN 'NONE'
                               ELSE 'PARTIAL'
                           END AS refund_tier
                    FROM refunds
                    WHERE patient_id = %s
                    ORDER BY created_at DESC, refund_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (patient["patient_id"], limit + 1, offset),
                )
                refunds = cursor.fetchall()
                has_more = len(refunds) > limit
                return {
                    "refunds": refunds[:limit],
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "next_offset": offset + limit if has_more else None,
                }
        finally:
            connection.close()

    def list_appointments(self, role, user_id, limit=50, offset=0):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                scope, params = self._appointment_scope(cursor, role, user_id)
                cursor.execute(
                    f"""
                    SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id,
                           a.consultation_type_id,
                           COALESCE(a.slot_start_time, a.appointment_datetime)
                               AS slot_start_time,
                           COALESCE(a.status, 'BOOKED') AS status,
                           a.appointment_status, a.created_at,
                           ct.consultation_name AS consultation_type,
                           ct.duration_minutes, dct.fee
                    FROM appointments a
                    LEFT JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    LEFT JOIN doctor_consulting_types dct
                      ON dct.consultation_type_id = a.consultation_type_id
                     AND dct.doctor_id = a.doctor_id
                    {scope}
                    ORDER BY a.created_at DESC, a.appointment_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, limit + 1, offset),
                )
                rows = cursor.fetchall()
                has_more = len(rows) > limit
                items = rows[:limit]
                return {
                    "appointments": items,
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "next_offset": offset + limit if has_more else None,
                }
        finally:
            connection.close()

    def get_appointment(self, role, user_id, appointment_id):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                scope, params = self._appointment_scope(cursor, role, user_id)
                cursor.execute(
                    f"""
                    SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id,
                           a.consultation_type_id,
                           COALESCE(a.slot_start_time, a.appointment_datetime)
                               AS slot_start_time,
                           COALESCE(a.status, 'BOOKED') AS status,
                           a.appointment_status, a.created_at,
                           ct.consultation_name AS consultation_type,
                           ct.duration_minutes, dct.fee
                    FROM appointments a
                    LEFT JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    LEFT JOIN doctor_consulting_types dct
                      ON dct.consultation_type_id = a.consultation_type_id
                     AND dct.doctor_id = a.doctor_id
                    {scope}
                      AND a.appointment_id = %s
                    LIMIT 1
                    """,
                    (*params, appointment_id),
                )
                return cursor.fetchone()
        finally:
            connection.close()

    def _appointment_scope(self, cursor, role, user_id):
        if role == "ADMIN":
            return "WHERE 1 = 1", ()
        if role == "PATIENT":
            cursor.execute(
                "SELECT patient_id FROM patients WHERE user_id = %s",
                (user_id,),
            )
            record = cursor.fetchone()
            if not record:
                raise AppointmentRepositoryError(
                    404, "Patient profile was not found."
                )
            return "WHERE a.patient_id = %s", (record["patient_id"],)
        if role == "DOCTOR":
            cursor.execute(
                "SELECT doctor_id FROM doctors WHERE user_id = %s",
                (user_id,),
            )
            record = cursor.fetchone()
            if not record:
                raise AppointmentRepositoryError(
                    404, "Doctor profile was not found."
                )
            return "WHERE a.doctor_id = %s", (record["doctor_id"],)
        raise AppointmentRepositoryError(403, "Role is not allowed.")

def get_parameter(name, decrypt=False):
    if name not in parameter_cache:
        result = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        parameter_cache[name] = result["Parameter"]["Value"]
    return parameter_cache[name]


def get_connection():
    return pymysql.connect(
        host=get_parameter(f"/medislot/{ENVIRONMENT}/rds-host"),
        port=int(get_parameter(f"/medislot/{ENVIRONMENT}/rds-port")),
        user=get_parameter(f"/medislot/{ENVIRONMENT}/rds-user"),
        password=get_parameter(
            f"/medislot/{ENVIRONMENT}/rds-password",
            decrypt=True,
        ),
        database=get_parameter(f"/medislot/{ENVIRONMENT}/rds-database"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
