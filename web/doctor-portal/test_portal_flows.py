import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SECRET_KEY", "portal-test-key")
os.environ.setdefault("MEDISLOT_API_BASE_URL", "https://example.invalid/dev")

from app import app


class PortalAuthTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def csrf_token(self, path="/auth"):
        self.client.get(path)
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]

    def test_unified_auth_page_renders_both_modes_and_roles(self):
        response = self.client.get("/auth")
        registration = self.client.get("/auth?mode=register&role=PATIENT")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(registration.status_code, 200)
        self.assertIn(b"Patient", response.data)
        self.assertIn(b"Doctor", response.data)
        self.assertIn(b"Registration", response.data)

    @patch("app.api_request")
    def test_registration_submits_selected_role(self, api_request):
        csrf = self.csrf_token("/auth?mode=register&role=PATIENT")
        response = self.client.post(
            "/auth",
            data={
                "csrf_token": csrf,
                "mode": "register",
                "role": "PATIENT",
                "first_name": "Mina",
                "last_name": "Shah",
                "email": "mina@example.com",
                "password": "SecurePatient123",
                "confirm_password": "SecurePatient123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth?mode=login&role=PATIENT", response.headers["Location"])
        self.assertEqual(api_request.call_args.kwargs["json_body"]["role"], "PATIENT")

    @patch("app.api_request")
    def test_patient_login_redirects_to_patient_dashboard(self, api_request):
        api_request.return_value = {
            "access_token": "patient-access",
            "refresh_token": "patient-refresh",
            "role": "PATIENT",
        }
        response = self.client.post(
            "/auth",
            data={
                "csrf_token": self.csrf_token(),
                "mode": "login",
                "role": "PATIENT",
                "email": "mina@example.com",
                "password": "SecurePatient123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/patient")
        self.assertEqual(self.client.get("/dashboard/patient").status_code, 200)

    @patch("app.api_request")
    def test_doctor_login_redirects_to_doctor_dashboard(self, api_request):
        api_request.return_value = {
            "access_token": "doctor-access",
            "refresh_token": "doctor-refresh",
            "role": "DOCTOR",
        }
        response = self.client.post(
            "/auth",
            data={
                "csrf_token": self.csrf_token(),
                "mode": "login",
                "role": "DOCTOR",
                "email": "doctor@example.com",
                "password": "SecureDoctor123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dashboard/doctor")


class DoctorDashboardTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    @patch("app.api_request")
    def test_dashboard_defaults_to_first_sidebar_view_and_renders_profile(self, api_request):
        profile = {
            "first_name": "Asha", "last_name": "Rao", "email": "asha@example.com",
            "phone_number": "+15550001111", "license_number": "LIC-123",
            "experience_years": 8, "qualification": "MBBS", "languages": "English",
            "hospital_name": "City Clinic", "city": "Springfield", "address": "1 Main Street",
            "hospital_phone": "+15550002222", "specialization_names": ["Cardiology"],
            "available_specializations": [], "is_verified": True,
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
                return {"date": "2026-11-04", "slots": []}
            raise AssertionError(path)

        api_request.side_effect = fake_api_request
        with self.client.session_transaction() as browser_session:
            browser_session["access_token"] = "test-access-token"
            browser_session["role"] = "DOCTOR"

        response = self.client.get("/dashboard/doctor")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dr. Asha Rao", response.data)
        self.assertIn(b"Cardiology", response.data)
        self.assertIn(b"https://photos.example/profile.jpg", response.data)
        self.assertIn(b'id="dashboard-view" data-view', response.data)

    @patch("app.api_request")
    def test_profile_update_sends_specializations(self, api_request):
        csrf = self.csrf_token()
        with self.client.session_transaction() as browser_session:
            browser_session["access_token"] = "test-access-token"
            browser_session["role"] = "DOCTOR"
        response = self.client.post(
            "/doctor/profile",
            data={
                "csrf_token": csrf,
                "first_name": "Asha",
                "last_name": "Rao",
                "experience_years": "8",
                "specializations": "Cardiology, Neurology",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(api_request.call_args.kwargs["json_body"]["specializations"], "Cardiology, Neurology")

    def csrf_token(self):
        self.client.get("/auth")
        with self.client.session_transaction() as browser_session:
            return browser_session["csrf_token"]


if __name__ == "__main__":
    unittest.main()
