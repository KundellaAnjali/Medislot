import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, ROUND_HALF_UP

import boto3
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)
parameter_cache = {}
ssm = None


class RefundServiceError(Exception):
    pass


class RefundService:
    PARAMETER_NAMES = {
        "more_than_48_hours_percent": "REFUND_MORE_THAN_48_HOURS_PERCENT",
        "24_to_48_hours_percent": "REFUND_24_TO_48_HOURS_PERCENT",
        "6_to_24_hours_percent": "REFUND_6_TO_24_HOURS_PERCENT",
        "less_than_6_hours_percent": "REFUND_LESS_THAN_6_HOURS_PERCENT",
        "doctor_cancellation_percent": "REFUND_DOCTOR_CANCELLATION_PERCENT",
        "no_show_percent": "REFUND_NO_SHOW_PERCENT",
    }

    def __init__(self, parameter_reader=None):
        self.parameter_reader = parameter_reader or self._read_parameter
        self.configuration = None

    @staticmethod
    def _read_parameter(name):
        global ssm
        if name not in parameter_cache:
            environment = os.environ.get("ENVIRONMENT_NAME")
            if not environment:
                raise RefundServiceError(
                    "Refund configuration environment is not set."
                )
            if ssm is None:
                ssm = boto3.client("ssm")
            try:
                result = ssm.get_parameter(
                    Name=f"/medislot/{environment}/{name}"
                )
            except (BotoCoreError, ClientError) as error:
                raise RefundServiceError(
                    f"Could not load refund configuration parameter {name}."
                ) from error
            parameter_cache[name] = result["Parameter"]["Value"]
        return parameter_cache[name]

    def _get_configuration(self):
        if self.configuration is not None:
            return self.configuration
        try:
            config = {
                "48_hour_threshold": int(
                    self.parameter_reader("REFUND_48_HOUR_THRESHOLD")
                ),
                "24_hour_threshold": int(
                    self.parameter_reader("REFUND_24_HOUR_THRESHOLD")
                ),
                "6_hour_threshold": int(
                    self.parameter_reader("REFUND_6_HOUR_THRESHOLD")
                ),
            }
            config.update(
                {
                    key: Decimal(self.parameter_reader(parameter_name))
                    for key, parameter_name in self.PARAMETER_NAMES.items()
                }
            )
        except RefundServiceError:
            raise
        except (
            BotoCoreError,
            ClientError,
            DecimalException,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            logger.exception("Invalid appointment refund configuration in SSM")
            raise RefundServiceError(
                "Refund processing is temporarily unavailable."
            ) from error

        percentages = (
            config["more_than_48_hours_percent"],
            config["24_to_48_hours_percent"],
            config["6_to_24_hours_percent"],
            config["less_than_6_hours_percent"],
        )
        if (
            config["48_hour_threshold"] <= config["24_hour_threshold"]
            or config["24_hour_threshold"] <= config["6_hour_threshold"]
            or config["6_hour_threshold"] <= 0
            or any(not value.is_finite() or value < 0 or value > 100 for value in percentages)
            or any(
                earlier < later
                for earlier, later in zip(percentages, percentages[1:])
            )
            or not config["doctor_cancellation_percent"].is_finite()
            or not Decimal("0")
            <= config["doctor_cancellation_percent"]
            <= Decimal("100")
            or not config["no_show_percent"].is_finite()
            or not Decimal("0") <= config["no_show_percent"] <= Decimal("100")
        ):
            raise RefundServiceError(
                "Refund processing is temporarily unavailable."
            )
        self.configuration = config
        return config

    def calculate(
        self,
        appointment_datetime,
        cancellation_time,
        paid_amount,
        *,
        doctor_cancel=False,
        no_show=False,
    ):
        if doctor_cancel and no_show:
            raise ValueError("A no-show cannot also be a doctor cancellation.")

        config = self._get_configuration()
        if no_show:
            percentage = config["no_show_percent"]
            tier = "NONE"
            reason = "PATIENT_NO_SHOW"
        elif doctor_cancel:
            percentage = config["doctor_cancellation_percent"]
            tier = "FULL" if percentage == 100 else "PARTIAL" if percentage else "NONE"
            reason = "DOCTOR_SLOT_CANCELLATION"
        else:
            appointment_time = self._as_utc(appointment_datetime)
            cancelled_at = self._as_utc(cancellation_time)
            time_until = appointment_time - cancelled_at
            if time_until.total_seconds() > config["48_hour_threshold"] * 3600:
                percentage = config["more_than_48_hours_percent"]
                tier = "FULL" if percentage == 100 else "PARTIAL" if percentage else "NONE"
                reason = "PATIENT_CANCELLATION_MORE_THAN_48_HOURS"
            elif time_until.total_seconds() > config["24_hour_threshold"] * 3600:
                percentage = config["24_to_48_hours_percent"]
                tier = "FULL" if percentage == 100 else "PARTIAL" if percentage else "NONE"
                reason = "PATIENT_CANCELLATION_24_TO_48_HOURS"
            elif time_until.total_seconds() >= config["6_hour_threshold"] * 3600:
                percentage = config["6_to_24_hours_percent"]
                tier = "FULL" if percentage == 100 else "PARTIAL" if percentage else "NONE"
                reason = "PATIENT_CANCELLATION_6_TO_24_HOURS"
            else:
                percentage = config["less_than_6_hours_percent"]
                tier = "FULL" if percentage == 100 else "PARTIAL" if percentage else "NONE"
                reason = "PATIENT_CANCELLATION_LESS_THAN_6_HOURS"

        paid = Decimal(str(paid_amount))
        if not paid.is_finite() or paid < 0:
            raise ValueError("Paid amount must be a non-negative finite amount.")
        refund_amount = (paid * percentage / Decimal("100")).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        deduction_amount = (paid - refund_amount).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        return {
            "refund_amount": refund_amount,
            "deduction_amount": deduction_amount,
            "refund_percentage": percentage,
            "refund_tier": tier,
            "refund_reason": reason,
        }

    @staticmethod
    def _as_utc(value):
        if not isinstance(value, datetime):
            raise TypeError("Appointment and cancellation times must be datetimes.")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
