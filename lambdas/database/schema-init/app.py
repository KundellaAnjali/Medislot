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


def lambda_handler(event, context):
    started_at = perf_counter()
    request_id = getattr(context, "aws_request_id", "unknown")

    log_event(
        "INFO",
        "SchemaInitializationStarted",
        {
            "environment": ENVIRONMENT,
            "requestId": request_id,
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
                "executedStatements": executed_statements
            }
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Schema initialized successfully",
                    "executed_statements": executed_statements
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
                "error": str(exception),
                "errorType": type(exception).__name__,
                "requestId": request_id,
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