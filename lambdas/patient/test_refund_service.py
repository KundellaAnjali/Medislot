import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    from .refund_service import RefundService
except ImportError:
    from refund_service import RefundService


CONFIG = {
    "REFUND_48_HOUR_THRESHOLD": "48",
    "REFUND_24_HOUR_THRESHOLD": "24",
    "REFUND_6_HOUR_THRESHOLD": "6",
    "REFUND_MORE_THAN_48_HOURS_PERCENT": "100",
    "REFUND_24_TO_48_HOURS_PERCENT": "75",
    "REFUND_6_TO_24_HOURS_PERCENT": "50",
    "REFUND_LESS_THAN_6_HOURS_PERCENT": "0",
    "REFUND_DOCTOR_CANCELLATION_PERCENT": "100",
    "REFUND_NO_SHOW_PERCENT": "0",
}


class RefundServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = RefundService(parameter_reader=CONFIG.__getitem__)
        self.cancellation_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def calculate_at_hours(self, hours, **options):
        return self.service.calculate(
            self.cancellation_time + timedelta(hours=hours),
            self.cancellation_time,
            Decimal("200.00"),
            **options,
        )

    def test_refund_tiers_and_exact_boundaries(self):
        cases = (
            (48.0001, Decimal("100")),
            (48, Decimal("75")),
            (24.0001, Decimal("75")),
            (24, Decimal("50")),
            (6, Decimal("50")),
            (5.9999, Decimal("0")),
        )
        for hours, expected_percentage in cases:
            with self.subTest(hours=hours):
                result = self.calculate_at_hours(hours)
                self.assertEqual(result["refund_percentage"], expected_percentage)
                self.assertEqual(
                    result["refund_amount"],
                    Decimal("200.00") * expected_percentage / Decimal("100"),
                )
                self.assertEqual(
                    result["refund_amount"] + result["deduction_amount"],
                    Decimal("200.00"),
                )

    def test_doctor_cancellation_ignores_timing(self):
        result = self.calculate_at_hours(1, doctor_cancel=True)

        self.assertEqual(result["refund_percentage"], Decimal("100"))
        self.assertEqual(result["refund_amount"], Decimal("200.00"))
        self.assertEqual(result["deduction_amount"], Decimal("0.00"))

    def test_no_show_has_no_refund(self):
        result = self.calculate_at_hours(72, no_show=True)

        self.assertEqual(result["refund_percentage"], Decimal("0"))
        self.assertEqual(result["refund_amount"], Decimal("0.00"))
        self.assertEqual(result["deduction_amount"], Decimal("200.00"))
        self.assertEqual(result["refund_reason"], "PATIENT_NO_SHOW")


if __name__ == "__main__":
    unittest.main()
