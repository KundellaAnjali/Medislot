import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import pymysql


logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]
MAX_ACTIVE_APPOINTMENTS_PER_DOCTOR = 3
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
        payment_method,
        payment_outcome,
        reserve_slot,
        release_slot,
    ):
        connection = self.connection_factory()
        reserved_slot = None
        commit_started = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.patient_id, p.first_name, u.email
                    FROM patients p
                    JOIN users u ON u.user_id = p.user_id
                    WHERE p.user_id = %s
                    FOR UPDATE
                    """,
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
                      AND doctor_id = %s
                      AND slot_start_time > UTC_TIMESTAMP(6)
                      AND (status = 'BOOKED' OR appointment_status = 'CONFIRMED')
                    """,
                    (patient_id, doctor_id),
                )
                active_count = int(cursor.fetchone()["active_count"])
                if active_count >= MAX_ACTIVE_APPOINTMENTS_PER_DOCTOR:
                    raise AppointmentRepositoryError(
                        409,
                        f"You can have up to {MAX_ACTIVE_APPOINTMENTS_PER_DOCTOR} upcoming active appointments with this doctor.",
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
                transaction_id = f"PAY-{uuid.uuid4().hex.upper()}"
                appointment_status = (
                    "CONFIRMED" if payment_outcome == "SUCCESS" else "CANCELLED"
                )
                booking_status = (
                    "BOOKED" if payment_outcome == "SUCCESS" else "CANCELLED"
                )

                cursor.execute(
                    """
                    INSERT INTO appointments
                        (patient_id, doctor_id, slot_id, consultation_type_id,
                         appointment_status, appointment_datetime,
                         slot_start_time, status, cancelled_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                    """,
                    (
                        patient_id,
                        doctor_id,
                        slot_id,
                        consultation_type_id,
                        appointment_status,
                        appointment_start,
                        appointment_start,
                        booking_status,
                        "SYSTEM" if booking_status == "CANCELLED" else None,
                    ),
                )
                appointment_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    INSERT INTO payments
                        (appointment_id, amount, transaction_id,
                         payment_method, status, created_at, paid_at)
                    VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6),
                            CASE WHEN %s = 'SUCCESS' THEN UTC_TIMESTAMP(6) ELSE NULL END)
                    """,
                    (
                        appointment_id,
                        consultation["fee"],
                        transaction_id,
                        payment_method,
                        payment_outcome,
                        payment_outcome,
                    ),
                )
                if payment_outcome == "FAILED":
                    failed_slot_reservation = reserved_slot
                    reserved_slot = None
                    release_slot(failed_slot_reservation)
                response_payload = {
                    "appointment_id": appointment_id,
                    "patient_id": patient_id,
                    "doctor_id": doctor_id,
                    "slot_id": slot_id,
                    "_notification": {
                        "email": patient["email"],
                        "first_name": patient["first_name"],
                    },
                    "slot_start_time": slot_start_time,
                    "message": (
                        "Test payment failed; the slot reservation was released."
                        if payment_outcome == "FAILED"
                        else "Test payment succeeded."
                    ),
                    "status": (
                        "BOOKED" if payment_outcome == "SUCCESS" else "PAYMENT_FAILED"
                    ),
                    "appointment_status": appointment_status,
                    "payment_method": payment_method,
                    "payment_status": payment_outcome,
                    "transaction_id": transaction_id,
                    "booked_count": (
                        int(reserved_slot["booked_count"])
                        if reserved_slot is not None
                        else None
                    ),
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
        refund_service,
        now,
        deactivate_slot=None,
        single_appointment=False,
    ):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT appointment_id, patient_id, doctor_id, slot_id,
                           COALESCE(slot_start_time, appointment_datetime)
                               AS slot_start_time,
                           COALESCE(status, 'BOOKED') AS status,
                           appointment_status
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
                    appointment_time = selected["slot_start_time"]
                    if appointment_time is not None and appointment_time.tzinfo is None:
                        appointment_time = appointment_time.replace(
                            tzinfo=timezone.utc
                        )
                    if appointment_time is not None and appointment_time <= now:
                        raise AppointmentRepositoryError(
                            409, "Past appointments cannot be cancelled."
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
                    if single_appointment and (
                        selected["status"] != "BOOKED"
                        or selected["appointment_status"] != "CONFIRMED"
                    ):
                        raise AppointmentRepositoryError(
                            409, "Only a booked appointment can be cancelled individually."
                        )
                    if deactivate_slot and not single_appointment:
                        deactivate_slot(
                            int(selected["doctor_id"]),
                            selected["slot_start_time"],
                        )
                    if single_appointment:
                        targets = [selected]
                    else:
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
                    cursor.execute(
                        """
                        SELECT p.first_name, u.email
                        FROM patients p
                        JOIN users u ON u.user_id = p.user_id
                        WHERE p.patient_id = %s
                        """,
                        (appointment["patient_id"],),
                    )
                    patient_notification = cursor.fetchone()
                    if not patient_notification:
                        raise AppointmentRepositoryError(
                            404, "Patient profile was not found."
                        )

                    if appointment["status"] == "CANCELLED":
                        cursor.execute(
                            """
                            SELECT refund_amount, deduction_amount,
                                   refund_percentage, refund_reason
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
                                "deduction_amount": str(
                                    existing_refund["deduction_amount"]
                                ),
                                "refund_percentage": str(percentage),
                                "refund_reason": existing_refund["refund_reason"],
                                "refund_tier": tier,
                                "_notification": {
                                    "email": patient_notification["email"],
                                    "first_name": patient_notification["first_name"],
                                },
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
                    refund = refund_service.calculate(
                        appointment_time,
                        now,
                        paid_amount,
                        doctor_cancel=doctor_cancel,
                    )
                    cursor.execute(
                        """
                        UPDATE appointments
                        SET status = 'CANCELLED', appointment_status = 'CANCELLED',
                            cancelled_by = %s
                        WHERE appointment_id = %s AND status = 'BOOKED'
                        """,
                        (role, appointment["appointment_id"]),
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
                             deduction_amount,
                             refund_percentage, refund_transaction_id,
                             refund_reason, status, created_at, refunded_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6),
                                CASE WHEN %s = 'SUCCESS' THEN UTC_TIMESTAMP(6) ELSE NULL END)
                        """,
                        (
                            appointment["appointment_id"],
                            appointment["patient_id"],
                            refund["refund_amount"],
                            refund["deduction_amount"],
                            refund["refund_percentage"],
                            (
                                f"REF-{uuid.uuid4().hex.upper()}"
                                if refund["refund_amount"] > 0
                                else None
                            ),
                            refund["refund_reason"],
                            (
                                "SUCCESS"
                                if refund["refund_amount"] > 0
                                else "NOT_REQUIRED"
                            ),
                            (
                                "SUCCESS"
                                if refund["refund_amount"] > 0
                                else "NOT_REQUIRED"
                            ),
                        ),
                    )
                    if refund["refund_amount"] > 0:
                        cursor.execute(
                            """
                            UPDATE payments
                            SET status = 'REFUNDED'
                            WHERE appointment_id = %s
                              AND status IN ('SUCCESS', 'PAID', 'COMPLETED', 'SIMULATED')
                            """,
                            (appointment["appointment_id"],),
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
                        "deduction_amount": str(refund["deduction_amount"]),
                        "refund_percentage": str(refund["refund_percentage"]),
                        "refund_reason": refund["refund_reason"],
                        "_notification": {
                            "email": patient_notification["email"],
                            "first_name": patient_notification["first_name"],
                        },
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

    def mark_no_show(self, user_id, appointment_id, refund_service, now):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT appointment_id, patient_id, doctor_id,
                           COALESCE(slot_start_time, appointment_datetime)
                               AS appointment_datetime,
                           COALESCE(status, 'BOOKED') AS status,
                           appointment_status
                    FROM appointments
                    WHERE appointment_id = %s
                    FOR UPDATE
                    """,
                    (appointment_id,),
                )
                appointment = cursor.fetchone()
                if not appointment:
                    raise AppointmentRepositoryError(
                        404, "Appointment was not found."
                    )

                cursor.execute(
                    "SELECT doctor_id FROM doctors WHERE user_id = %s",
                    (user_id,),
                )
                doctor = cursor.fetchone()
                if not doctor:
                    raise AppointmentRepositoryError(
                        404, "Doctor profile was not found."
                    )
                if int(doctor["doctor_id"]) != int(appointment["doctor_id"]):
                    raise AppointmentRepositoryError(
                        404, "Appointment was not found."
                    )
                if appointment["status"] != "BOOKED" or (
                    appointment["appointment_status"] != "CONFIRMED"
                ):
                    raise AppointmentRepositoryError(
                        409, "Only a booked appointment can be marked as a no-show."
                    )

                appointment_datetime = appointment["appointment_datetime"]
                if appointment_datetime.tzinfo is None:
                    appointment_datetime = appointment_datetime.replace(
                        tzinfo=timezone.utc
                    )
                if appointment_datetime > now:
                    raise AppointmentRepositoryError(
                        409,
                        "An appointment cannot be marked as a no-show before it starts.",
                    )

                cursor.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS paid_amount
                    FROM payments
                    WHERE appointment_id = %s
                      AND (status IN ('PAID', 'COMPLETED', 'SUCCESS', 'REFUNDED')
                           OR (status = 'SIMULATED'
                               AND payment_method = 'SIMULATED'))
                    """,
                    (appointment_id,),
                )
                paid_amount = cursor.fetchone()["paid_amount"]
                refund = refund_service.calculate(
                    appointment_datetime,
                    now,
                    paid_amount,
                    no_show=True,
                )

                cursor.execute(
                    """
                    UPDATE appointments
                    SET status = 'NO_SHOW', appointment_status = 'NO_SHOW'
                    WHERE appointment_id = %s
                      AND status = 'BOOKED'
                      AND appointment_status = 'CONFIRMED'
                    """,
                    (appointment_id,),
                )
                if cursor.rowcount != 1:
                    raise AppointmentRepositoryError(
                        409, "Appointment changed while no-show was being recorded."
                    )

                cursor.execute(
                    """
                    INSERT INTO refunds
                        (appointment_id, patient_id, refund_amount,
                         deduction_amount, refund_percentage,
                         refund_transaction_id, refund_reason, status,
                         created_at, refunded_at)
                    VALUES (%s, %s, %s, %s, %s, NULL, %s, 'NOT_REQUIRED',
                            UTC_TIMESTAMP(6), NULL)
                    """,
                    (
                        appointment_id,
                        appointment["patient_id"],
                        refund["refund_amount"],
                        refund["deduction_amount"],
                        refund["refund_percentage"],
                        refund["refund_reason"],
                    ),
                )

            connection.commit()
            return {
                "appointment_id": appointment_id,
                "status": "NO_SHOW",
                "refund_amount": refund["refund_amount"],
                "deduction_amount": refund["deduction_amount"],
                "refund_percentage": refund["refund_percentage"],
                "refund_reason": refund["refund_reason"],
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_patient_notification(self, patient_id):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT p.first_name, u.email
                    FROM patients p
                    JOIN users u ON u.user_id = p.user_id
                    WHERE p.patient_id = %s
                    """,
                    (patient_id,),
                )
                patient = cursor.fetchone()
                if not patient:
                    raise AppointmentRepositoryError(
                        404, "Patient profile was not found."
                    )
                return {
                    "email": patient["email"],
                    "first_name": patient["first_name"],
                }
        finally:
            connection.close()

    def complete_elapsed_appointments(self):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE appointments AS a
                    JOIN consultation_types AS ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    SET a.status = 'COMPLETED',
                        a.appointment_status = 'COMPLETED'
                    WHERE a.status = 'BOOKED'
                      AND a.appointment_status = 'CONFIRMED'
                      AND DATE_ADD(
                          COALESCE(a.slot_start_time, a.appointment_datetime),
                          INTERVAL ct.duration_minutes MINUTE
                      ) <= UTC_TIMESTAMP(6)
                    """
                )
                completed_count = cursor.rowcount
            connection.commit()
            logger.info(
                "Automatically completed elapsed appointments: count=%s",
                completed_count,
            )
            return completed_count
        except Exception:
            connection.rollback()
            logger.exception("Could not automatically complete elapsed appointments")
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
                           deduction_amount, refund_percentage, refund_transaction_id,
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

    def list_appointments(
        self,
        role,
        user_id,
        limit=50,
        offset=0,
        appointment_date=None,
        appointment_status=None,
        search=None,
    ):
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                scope, params = self._appointment_scope(cursor, role, user_id)
                filters = []
                filter_params = []
                if appointment_date:
                    filters.append(
                        "DATE(COALESCE(a.slot_start_time, a.appointment_datetime)) = %s"
                    )
                    filter_params.append(appointment_date)
                if appointment_status == "booked":
                    filters.append(
                        "COALESCE(a.status, 'BOOKED') = 'BOOKED' "
                        "AND a.appointment_status = 'CONFIRMED'"
                    )
                elif appointment_status == "cancelled":
                    filters.append(
                        "(a.status IN ('CANCELLED', 'CANCELED') "
                        "OR a.appointment_status IN ('CANCELLED', 'CANCELED'))"
                    )
                elif appointment_status == "completed":
                    filters.append(
                        "(a.status = 'COMPLETED' "
                        "OR a.appointment_status = 'COMPLETED')"
                    )
                elif appointment_status == "no_show":
                    filters.append(
                        "(a.status = 'NO_SHOW' "
                        "OR a.appointment_status = 'NO_SHOW')"
                    )
                elif appointment_status == "all":
                    filters.append(
                        "(COALESCE(a.status, 'BOOKED') = 'BOOKED' "
                        "AND a.appointment_status = 'CONFIRMED') "
                        "OR a.status IN ('CANCELLED', 'CANCELED') "
                        "OR a.appointment_status IN ('CANCELLED', 'CANCELED') "
                        "OR a.status IN ('COMPLETED', 'NO_SHOW') "
                        "OR a.appointment_status IN ('COMPLETED', 'NO_SHOW')"
                    )
                if search:
                    filters.append(
                        "(CONCAT_WS(' ', patient.first_name, patient.last_name) "
                        "LIKE %s OR CAST(a.appointment_id AS CHAR) LIKE %s)"
                    )
                    search_term = f"%{search}%"
                    filter_params.extend((search_term, search_term))
                if filters:
                    scope += " AND " + " AND ".join(
                        f"({filter_expression})" for filter_expression in filters
                    )
                cursor.execute(
                    f"""
                    SELECT a.appointment_id, a.patient_id, a.doctor_id, a.slot_id,
                           a.consultation_type_id,
                           COALESCE(a.slot_start_time, a.appointment_datetime)
                               AS slot_start_time,
                           COALESCE(a.status, 'BOOKED') AS status,
                           a.appointment_status, a.cancelled_by, a.created_at,
                           ct.consultation_name AS consultation_type,
                           p.status AS payment_status,
                           p.payment_method,
                           p.amount AS amount_paid,
                           p.transaction_id,
                           r.status AS refund_status,
                           r.refund_amount,
                           r.deduction_amount,
                           r.refund_percentage,
                           r.refund_transaction_id,
                           r.refunded_at,
                           ct.duration_minutes,
                           COALESCE(p.amount, dct.fee) AS fee,
                           patient.first_name AS patient_first_name,
                           patient.last_name AS patient_last_name,
                           patient.phone_number AS patient_phone_number,
                           patient.emergency_contact AS patient_emergency_contact,
                           patient.date_of_birth AS patient_date_of_birth,
                           patient.gender AS patient_gender,
                           patient_user.email AS patient_email,
                           COALESCE(
                               NULLIF(
                                   TRIM(CONCAT_WS(' ', doctor.first_name, doctor.last_name)),
                                   ''
                               ),
                               CONCAT('Doctor ', a.doctor_id)
                           ) AS doctor_name
                    FROM appointments a
                    LEFT JOIN patients patient
                      ON patient.patient_id = a.patient_id
                    LEFT JOIN users patient_user
                      ON patient_user.user_id = patient.user_id
                    LEFT JOIN doctors doctor
                      ON doctor.doctor_id = a.doctor_id
                    LEFT JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    LEFT JOIN doctor_consulting_types dct
                      ON dct.consultation_type_id = a.consultation_type_id
                     AND dct.doctor_id = a.doctor_id
                    LEFT JOIN payments p
                      ON p.appointment_id = a.appointment_id
                    LEFT JOIN refunds r
                      ON r.appointment_id = a.appointment_id
                    {scope}
                    ORDER BY a.created_at DESC, a.appointment_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, *filter_params, limit + 1, offset),
                )
                rows = cursor.fetchall()
                has_more = len(rows) > limit
                items = rows[:limit]
                now = datetime.now(timezone.utc)
                for item in items:
                    slot_start = item.get("slot_start_time")
                    if isinstance(slot_start, str):
                        try:
                            slot_start = datetime.fromisoformat(
                                slot_start.replace("Z", "+00:00")
                            )
                        except ValueError:
                            slot_start = None
                    if slot_start is not None:
                        if slot_start.tzinfo is None:
                            slot_start = slot_start.replace(tzinfo=timezone.utc)
                        item["is_past"] = slot_start <= now
                        duration_minutes = item.get("duration_minutes")
                        item["is_ended"] = (
                            slot_start + timedelta(minutes=duration_minutes) <= now
                            if duration_minutes is not None
                            else False
                        )
                    else:
                        item["is_past"] = False
                        item["is_ended"] = False
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
                           a.appointment_status, a.cancelled_by, a.created_at,
                           ct.consultation_name AS consultation_type,
                           p.status AS payment_status,
                           p.payment_method,
                           p.transaction_id,
                           r.status AS refund_status,
                           r.refund_amount,
                           r.deduction_amount,
                           r.refund_percentage,
                           r.refund_transaction_id,
                           r.refunded_at,
                           ct.duration_minutes, dct.fee
                    FROM appointments a
                    LEFT JOIN consultation_types ct
                      ON ct.consultation_type_id = a.consultation_type_id
                    LEFT JOIN doctor_consulting_types dct
                      ON dct.consultation_type_id = a.consultation_type_id
                     AND dct.doctor_id = a.doctor_id
                    LEFT JOIN payments p
                      ON p.appointment_id = a.appointment_id
                    LEFT JOIN refunds r
                      ON r.appointment_id = a.appointment_id
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
