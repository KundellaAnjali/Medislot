import os
import json
import logging
from time import perf_counter
from datetime import datetime, timezone

import boto3
import pymysql

ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

METRIC_NAMESPACE = "MediSlot/Database"


def publish_metric(metric_name, value, unit="Count"):

    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": metric_name,
                "Value": value,
                "Unit": unit
            }
        ]
    )


def log_event(
    level,
    event_name,
    details=None
):

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_name,
        "details": details or {}
    }

    if level == "ERROR":
        logger.error(json.dumps(payload))
    else:
        logger.info(json.dumps(payload))


def get_parameter(name, decrypt=False):

    response = ssm.get_parameter(
        Name=name,
        WithDecryption=decrypt
    )

    return response["Parameter"]["Value"]


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
        autocommit=True
    )


def ensure_index(cursor, table_name, index_name, columns):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        (table_name, index_name),
    )
    if cursor.fetchone()[0]:
        return False

    column_list = ", ".join(f"`{column}`" for column in columns)
    cursor.execute(
        f"ALTER TABLE `{table_name}` ADD INDEX `{index_name}` ({column_list})"
    )
    return True


def ensure_unique_index(cursor, table_name, index_name, columns):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        (table_name, index_name),
    )
    if cursor.fetchone()[0]:
        return False

    column_list = ", ".join(f"`{column}`" for column in columns)
    cursor.execute(
        f"ALTER TABLE `{table_name}` ADD UNIQUE INDEX `{index_name}` ({column_list})"
    )
    return True


def drop_index(cursor, table_name, index_name, supporting_index=None):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        (table_name, index_name),
    )
    if not cursor.fetchone()[0]:
        return False

    if supporting_index:
        support_name, support_columns = supporting_index
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = %s
              AND column_name = %s
              AND seq_in_index = 1
              AND index_name <> %s
            """,
            (table_name, support_columns[0], index_name),
        )
        if not cursor.fetchone()[0]:
            ensure_index(cursor, table_name, support_name, support_columns)

    cursor.execute(
        f"ALTER TABLE `{table_name}` DROP INDEX `{index_name}`"
    )
    return True


def table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = %s
        """,
        (table_name,),
    )
    return bool(cursor.fetchone()[0])


def migrate_simulated_payments(cursor):
    cursor.execute(
        """
        UPDATE payments
        SET transaction_id = CONCAT('PAY-', REPLACE(UUID(), '-', '')),
            payment_method = CASE MOD(payment_id, 6)
                WHEN 0 THEN 'CREDIT_CARD'
                WHEN 1 THEN 'DEBIT_CARD'
                WHEN 2 THEN 'UPI'
                WHEN 3 THEN 'NET_BANKING'
                WHEN 4 THEN 'WALLET'
                ELSE 'CASH'
            END,
            status = 'SUCCESS',
            paid_at = COALESCE(paid_at, created_at, UTC_TIMESTAMP(6))
        WHERE status = 'SIMULATED'
           OR payment_method = 'SIMULATED'
           OR transaction_id LIKE 'SIMULATED-%'
        """
    )
    migrated_rows = cursor.rowcount
    cursor.execute(
        """
        UPDATE payments p
        JOIN payments earlier
          ON earlier.transaction_id = p.transaction_id
         AND earlier.payment_id < p.payment_id
        SET p.transaction_id = CONCAT('PAY-', REPLACE(UUID(), '-', ''))
        """
    )
    migrated_rows += cursor.rowcount
    return migrated_rows


def migrate_pending_refunds(cursor):
    cursor.execute(
        """
        UPDATE refunds
        SET refund_transaction_id = CONCAT('REF-', REPLACE(UUID(), '-', '')),
            refunded_at = CASE
                WHEN status = 'PENDING' THEN UTC_TIMESTAMP(6)
                ELSE COALESCE(refunded_at, UTC_TIMESTAMP(6))
            END,
            status = 'SUCCESS'
        WHERE status IN ('PENDING', 'SUCCESS')
          AND refund_amount > 0
          AND (
              status = 'PENDING'
              OR refund_transaction_id IS NULL
              OR refunded_at IS NULL
          )
        """
    )
    migrated_rows = cursor.rowcount
    cursor.execute(
        """
        UPDATE payments p
        JOIN refunds r ON r.appointment_id = p.appointment_id
        SET p.status = 'REFUNDED'
        WHERE r.status = 'SUCCESS'
          AND r.refund_amount > 0
          AND p.status IN ('SUCCESS', 'PAID', 'COMPLETED', 'SIMULATED')
        """
    )
    migrated_rows += cursor.rowcount
    cursor.execute(
        """
        UPDATE refunds r
        JOIN refunds earlier
          ON earlier.refund_transaction_id = r.refund_transaction_id
         AND earlier.refund_id < r.refund_id
        SET r.refund_transaction_id = CONCAT('REF-', REPLACE(UUID(), '-', ''))
        """
    )
    migrated_rows += cursor.rowcount
    return migrated_rows


def migrate_refunds(cursor):
    cursor.execute("SHOW COLUMNS FROM refunds")
    columns = {column[0] for column in cursor.fetchall()}
    if "appointment_id" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN appointment_id INT NULL"
        )
    if "patient_id" not in columns:
        cursor.execute("ALTER TABLE refunds ADD COLUMN patient_id INT NULL")
    if "refund_reason" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN refund_reason VARCHAR(100) NULL"
        )
    if "refund_transaction_id" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN refund_transaction_id VARCHAR(255) NULL"
        )
    if "status" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN status VARCHAR(50) NOT NULL DEFAULT 'PENDING'"
        )
    if "created_at" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        )
    if "refunded_at" not in columns:
        cursor.execute(
            "ALTER TABLE refunds ADD COLUMN refunded_at TIMESTAMP NULL"
        )

    if "payment_id" in columns:
        cursor.execute(
            """
            SELECT CONSTRAINT_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'refunds'
              AND COLUMN_NAME = 'payment_id'
              AND REFERENCED_TABLE_NAME IS NOT NULL
            """
        )
        for constraint in cursor.fetchall():
            constraint_name = constraint[0].replace("`", "``")
            cursor.execute(
                f"ALTER TABLE refunds DROP FOREIGN KEY `{constraint_name}`"
            )
        cursor.execute(
            """
            UPDATE refunds r
            JOIN payments p ON p.payment_id = r.payment_id
            JOIN appointments a ON a.appointment_id = p.appointment_id
            SET r.appointment_id = p.appointment_id,
                r.patient_id = a.patient_id,
                r.refund_reason = COALESCE(r.refund_reason, 'REFUND')
            """
        )
        cursor.execute("ALTER TABLE refunds DROP COLUMN payment_id")

    if table_exists(cursor, "appointment_refund_ledger"):
        cursor.execute(
            """
            INSERT IGNORE INTO refunds
                (appointment_id, patient_id, refund_percentage, refund_amount,
                 refund_transaction_id, refund_reason, status, created_at,
                 refunded_at)
            SELECT appointment_id, patient_id, refund_percentage, refund_amount,
                   NULL, refund_reason,
                   CASE WHEN refund_amount > 0 THEN 'PENDING' ELSE 'NOT_REQUIRED' END,
                   created_at, NULL
            FROM appointment_refund_ledger
            """
        )

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM refunds
        WHERE appointment_id IS NULL OR patient_id IS NULL OR refund_reason IS NULL
        """
    )
    if cursor.fetchone()[0]:
        raise RuntimeError(
            "Existing refund rows could not be migrated to appointment and patient records."
        )

    cursor.execute(
        "ALTER TABLE refunds MODIFY appointment_id INT NOT NULL"
    )
    cursor.execute("ALTER TABLE refunds MODIFY patient_id INT NOT NULL")
    cursor.execute(
        "ALTER TABLE refunds MODIFY refund_reason VARCHAR(100) NOT NULL"
    )

    cursor.execute(
        """
        SELECT INDEX_NAME
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'refunds'
          AND COLUMN_NAME = 'appointment_id'
          AND NON_UNIQUE = 0
        LIMIT 1
        """
    )
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE refunds ADD UNIQUE KEY uq_refunds_appointment (appointment_id)"
        )

    refund_constraints = (
        ("fk_refund_appointment", "appointment_id", "appointments", "appointment_id"),
        ("fk_refund_patient", "patient_id", "patients", "patient_id"),
    )
    for constraint_name, column, referenced_table, referenced_column in refund_constraints:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.TABLE_CONSTRAINTS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'refunds'
              AND CONSTRAINT_NAME = %s
            """,
            (constraint_name,),
        )
        if not cursor.fetchone()[0]:
            cursor.execute(
                f"""
                ALTER TABLE refunds
                ADD CONSTRAINT `{constraint_name}`
                FOREIGN KEY (`{column}`)
                REFERENCES `{referenced_table}`(`{referenced_column}`)
                """
            )

    for legacy_table in (
        "appointment_cancellation_outbox",
        "appointment_outbox",
        "appointment_idempotency",
        "appointment_refund_ledger",
    ):
        if table_exists(cursor, legacy_table):
            cursor.execute(f"DROP TABLE `{legacy_table}`")


def lambda_handler(event, context):
    started_at = perf_counter()

    log_event(
        "INFO",
        "SchemaInitializationStarted",
        {
            "environment": ENVIRONMENT
        }
    )

    connection = None

    try:

        connection = get_connection()
        log_event(
            "INFO",
            "DatabaseConnectionEstablished"
        )

        with open("schema.sql", "r") as schema_file:
            schema = schema_file.read()

        with connection.cursor() as cursor:

            executed_statements = 0

            for statement in schema.split(";"):

                statement = statement.strip()

                if statement:

                    cursor.execute(statement)

                    executed_statements += 1

            migrate_refunds(cursor)
            if drop_index(
                cursor,
                "refunds",
                "idx_refunds_patient_created",
                ("idx_refunds_patient", ("patient_id",)),
            ):
                executed_statements += 1

            doctor_search_indexes = (
                (
                    "hospitals",
                    "idx_hospitals_city",
                    ("city",),
                ),
                (
                    "doctors",
                    "idx_doctors_hospital_id",
                    ("hospital_id",),
                ),
                (
                    "doctor_specializations",
                    "idx_doctor_specializations_specialization_doctor",
                    ("specialization_id", "doctor_id"),
                ),
            )
            for table_name, index_name, columns in doctor_search_indexes:
                if ensure_index(cursor, table_name, index_name, columns):
                    executed_statements += 1

            cursor.execute("SHOW COLUMNS FROM users LIKE 'role'")
            user_role_column = cursor.fetchone()
            if user_role_column and "admin" not in user_role_column[1].lower():
                cursor.execute(
                    """
                    ALTER TABLE users
                    MODIFY role ENUM('DOCTOR', 'PATIENT', 'ADMIN') NOT NULL
                    """
                )

            cursor.execute("SHOW COLUMNS FROM appointments")
            appointment_columns = {
                column[0] for column in cursor.fetchall()
            }
            if "slot_start_time" not in appointment_columns:
                cursor.execute(
                    "ALTER TABLE appointments ADD COLUMN slot_start_time DATETIME(6) NULL"
                )
                cursor.execute(
                    """
                    UPDATE appointments
                    SET slot_start_time = appointment_datetime
                    WHERE slot_start_time IS NULL
                    """
                )
            if "status" not in appointment_columns:
                cursor.execute(
                    """
                    ALTER TABLE appointments
                    ADD COLUMN status VARCHAR(32) NULL
                    """
                )
                cursor.execute(
                    """
                    UPDATE appointments
                    SET status = CASE
                        WHEN appointment_status = 'CONFIRMED' THEN 'BOOKED'
                        ELSE appointment_status
                    END
                    WHERE status IS NULL
                    """
                )
                cursor.execute(
                    """
                    ALTER TABLE appointments
                    MODIFY status VARCHAR(32) NOT NULL DEFAULT 'BOOKED'
                    """
                )
            if "cancelled_by" not in appointment_columns:
                cursor.execute(
                    "ALTER TABLE appointments ADD COLUMN cancelled_by VARCHAR(16) NULL"
                )

            appointment_indexes = (
                (
                    "appointments",
                    "idx_appointments_patient_created",
                    ("patient_id", "created_at", "appointment_id"),
                ),
                (
                    "appointments",
                    "idx_appointments_doctor_created",
                    ("doctor_id", "created_at", "appointment_id"),
                ),
            )
            for table_name, index_name, columns in appointment_indexes:
                if ensure_index(cursor, table_name, index_name, columns):
                    executed_statements += 1

            cursor.execute("SHOW COLUMNS FROM doctors LIKE 'timezone_name'")
            if cursor.fetchone():
                cursor.execute("ALTER TABLE doctors DROP COLUMN timezone_name")

            cursor.execute(
                "SHOW COLUMNS FROM appointments LIKE 'slot_id'"
            )
            slot_id_column = cursor.fetchone()
            if slot_id_column and "bigint" not in slot_id_column[1].lower():
                cursor.execute(
                    "ALTER TABLE appointments MODIFY slot_id BIGINT NOT NULL"
                )

            cursor.execute("SHOW COLUMNS FROM consultation_types")
            consultation_columns = {
                column[0] for column in cursor.fetchall()
            }
            if "doctor_id" in consultation_columns and "fee" in consultation_columns:
                if "is_active" in consultation_columns:
                    cursor.execute(
                        """
                        INSERT IGNORE INTO doctor_consulting_types
                            (consultation_type_id, doctor_id, fee, is_active)
                        SELECT consultation_type_id, doctor_id, fee, is_active
                        FROM consultation_types
                        """
                    )
                else:
                    cursor.execute(
                        """
                        INSERT IGNORE INTO doctor_consulting_types
                            (consultation_type_id, doctor_id, fee)
                        SELECT consultation_type_id, doctor_id, fee
                        FROM consultation_types
                        """
                    )

            cursor.execute("SHOW COLUMNS FROM availability_patterns")
            availability_columns = {
                column[0] for column in cursor.fetchall()
            }
            if "doctor_id" not in availability_columns:
                cursor.execute(
                    "ALTER TABLE availability_patterns ADD COLUMN doctor_id INT NULL"
                )

            if "doctor_id" in consultation_columns:
                cursor.execute(
                    """
                    UPDATE availability_patterns ap
                    JOIN consultation_types ct
                      ON ct.consultation_type_id = ap.consultation_type_id
                    SET ap.doctor_id = ct.doctor_id
                    WHERE ap.doctor_id IS NULL
                    """
                )
            else:
                cursor.execute(
                    """
                    UPDATE availability_patterns ap
                    JOIN doctor_consulting_types dct
                      ON dct.consultation_type_id = ap.consultation_type_id
                    SET ap.doctor_id = dct.doctor_id
                    WHERE ap.doctor_id IS NULL
                    """
                )

            cursor.execute(
                "SELECT COUNT(*) FROM availability_patterns WHERE doctor_id IS NULL"
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    "ALTER TABLE availability_patterns MODIFY doctor_id INT NOT NULL"
                )

            cursor.execute(
                """
                SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'availability_patterns'
                  AND CONSTRAINT_NAME = 'fk_pattern_doctor_consulting_type'
                """
            )
            if not cursor.fetchone():
                cursor.execute(
                    """
                    ALTER TABLE availability_patterns
                    ADD CONSTRAINT fk_pattern_doctor_consulting_type
                    FOREIGN KEY (consultation_type_id, doctor_id)
                    REFERENCES doctor_consulting_types(consultation_type_id, doctor_id)
                    """
                )

            if "doctor_id" in consultation_columns:
                cursor.execute(
                    """
                    SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'consultation_types'
                      AND COLUMN_NAME = 'doctor_id'
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                    """
                )
                for constraint in cursor.fetchall():
                    constraint_name = constraint[0].replace("`", "``")
                    cursor.execute(
                        f"ALTER TABLE consultation_types DROP FOREIGN KEY `{constraint_name}`"
                    )
                cursor.execute(
                    "ALTER TABLE consultation_types DROP COLUMN doctor_id"
                )
            if "fee" in consultation_columns:
                cursor.execute("ALTER TABLE consultation_types DROP COLUMN fee")
            if "is_active" in consultation_columns:
                cursor.execute("ALTER TABLE consultation_types DROP COLUMN is_active")

            if migrate_simulated_payments(cursor):
                executed_statements += 1
            if migrate_pending_refunds(cursor):
                executed_statements += 1

            if ensure_unique_index(
                cursor,
                "payments",
                "uq_payment_transaction_id",
                ("transaction_id",),
            ):
                executed_statements += 1
            if ensure_unique_index(
                cursor,
                "refunds",
                "uq_refund_transaction_id",
                ("refund_transaction_id",),
            ):
                executed_statements += 1

        publish_metric(
            "SchemaInitializationSuccess",
            1
        )
        publish_metric(
            "SchemaInitializationDurationMs",
            (perf_counter() - started_at) * 1000,
            "Milliseconds",
        )

        log_event(
            "INFO",
            "SchemaInitializationCompleted",
            {
                "executedStatements": executed_statements,
            }
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Schema initialized successfully",
                    "executed_statements": executed_statements,
                }
            )
        }

    except Exception as exception:

        publish_metric(
            "SchemaInitializationFailure",
            1
        )
        publish_metric(
            "SchemaInitializationDurationMs",
            (perf_counter() - started_at) * 1000,
            "Milliseconds",
        )

        log_event(
            "ERROR",
            "SchemaInitializationFailed",
            {
                "error": str(exception)
            }
        )

        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "message": "Schema initialization failed",
                    "error": str(exception)
                }
            )
        }

    finally:

        if connection:
            connection.close()