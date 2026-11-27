import os
import unittest

os.environ.setdefault("ENVIRONMENT_NAME", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from appointment_repository import AppointmentRepository


class FakeCursor:
    rowcount = 2

    def __init__(self):
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query):
        self.query = query


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class AppointmentCompletionTests(unittest.TestCase):
    def test_completes_only_confirmed_bookings_after_duration(self):
        connection = FakeConnection()
        repository = AppointmentRepository(connection_factory=lambda: connection)

        completed_count = repository.complete_elapsed_appointments()

        self.assertEqual(completed_count, 2)
        self.assertIn(
            "INTERVAL ct.duration_minutes MINUTE",
            connection.cursor_instance.query,
        )
        self.assertIn("<= UTC_TIMESTAMP(6)", connection.cursor_instance.query)
        self.assertIn("a.status = 'BOOKED'", connection.cursor_instance.query)
        self.assertIn(
            "a.appointment_status = 'CONFIRMED'",
            connection.cursor_instance.query,
        )
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
