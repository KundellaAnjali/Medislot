import os
import json
import uuid
import logging
from datetime import datetime

import boto3
import pymysql

ssm = boto3.client("ssm")
cloudwatch = boto3.client("cloudwatch")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENVIRONMENT = os.environ["ENVIRONMENT_NAME"]

METRIC_NAMESPACE = "MediSlot/Database"


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


def lambda_handler(event, context):

    correlation_id = str(uuid.uuid4())

    log_event(
        "INFO",
        correlation_id,
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
            correlation_id,
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

        publish_metric(
            "SchemaInitializationSuccess",
            1
        )

        log_event(
            "INFO",
            correlation_id,
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
                    "executed_statements": executed_statements,
                    "correlation_id": correlation_id
                }
            )
        }

    except Exception as exception:

        publish_metric(
            "SchemaInitializationFailure",
            1
        )

        log_event(
            "ERROR",
            correlation_id,
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
                    "error": str(exception),
                    "correlation_id": correlation_id
                }
            )
        }

    finally:

        if connection:
            connection.close()