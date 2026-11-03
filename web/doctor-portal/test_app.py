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
    def test_dashboard_shows_saved_profile_and_photo(self, api_request):
        profile = {
            "first_name": "Asha",
            "last_name": "Rao",
            "email": "asha@example.com",
            "phone_number": "+15550001111",
            "license_number": "LIC-123",
            "experience_years": 8,
            "qualification": "MBBS",
            "languages": "English",
            "hospital_name": "City Clinic",
            "city": "Springfield",
            "address": "1 Main Street",
            "hospital_phone": "+15550002222",
            "specialization_names": ["Cardiology"],
            "available_specializations": [],
            "is_verified": True,
            "profile_image_key": "doctors/14/profile.jpg",
            "profile_image_url": "https://photos.example/profile.jpg",
        }

        def fake_api_request(method, path, **kwargs):
            if path == "/doctors/me":
                return profile
            if path == "/doctors/me/consultation-types":
                return {"consultation_types": []}
            if path == "/doctors/me/availability":
                return {"patterns": []}
            if path == "/doctors/me/blackouts":
                return {"blackouts": []}
            if path == "/doctors/me/schedule":
                return {"date": "2026-11-03", "slots": []}
            raise AssertionError(path)

        api_request.side_effect = fake_api_request
        with self.client.session_transaction() as browser_session:
            browser_session["access_token"] = "test-access-token"
            browser_session["role"] = "DOCTOR"

        result = self.client.get("/doctor")

        self.assertEqual(result.status_code, 200)
        self.assertIn(b"Dr. Asha Rao", result.data)
        self.assertIn(b"Cardiology", result.data)
        self.assertIn(b"https://photos.example/profile.jpg", result.data)
        self.assertIn(b"Edit profile", result.data)

    @patch("app.api_request")
    def test_profile_update_sends_specializations(self, api_request):
        self.client.get("/doctor/login")
        with self.client.session_transaction() as browser_session:
            csrf = browser_session["csrf_token"]
            browser_session["access_token"] = "test-access-token"
            browser_session["role"] = "DOCTOR"

        result = self.client.post(
            "/doctor/profile",
            data={
                "csrf_token": csrf,
                "first_name": "Asha",
                "last_name": "Rao",
                "experience_years": "8",
                "specializations": "Cardiology, Neurology",
            },
        )

        self.assertEqual(result.status_code, 302)
        self.assertEqual(api_request.call_args.kwargs["json_body"]["specializations"], "Cardiology, Neurology")

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
