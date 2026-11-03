import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SECRET_KEY", "portal-test-key")
os.environ.setdefault("MEDISLOT_API_BASE_URL", "https://example.invalid/dev")

from app import app


class DoctorRegistrationTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_registration_pages_are_first_entry_points(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/doctor/register").status_code, 200)
        self.assertEqual(self.client.get("/doctor/login").status_code, 200)

    @patch("app.api_request")
    def test_registration_calls_api_with_doctor_role(self, api_request):
        self.client.get("/doctor/register")
        with self.client.session_transaction() as browser_session:
            csrf = browser_session["csrf_token"]

        result = self.client.post(
            "/doctor/register",
            data={
                "csrf_token": csrf,
                "first_name": "Asha",
                "last_name": "Rao",
                "email": "asha@example.com",
                "password": "SecureDoctor123",
                "confirm_password": "SecureDoctor123",
            },
        )

        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.headers["Location"], "/doctor/login")
        api_request.assert_called_once_with(
            "POST",
            "/auth/register",
            json_body={
                "role": "DOCTOR",
                "first_name": "Asha",
                "last_name": "Rao",
                "email": "asha@example.com",
                "password": "SecureDoctor123",
            },
            authenticated=False,
        )


if __name__ == "__main__":
    unittest.main()
