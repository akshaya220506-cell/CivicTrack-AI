"""Privacy regression tests run against isolated demo storage."""

import json
import os
import re
import secrets
import tempfile
import unittest
from unittest.mock import patch


_TEMP_STORAGE = tempfile.TemporaryDirectory(prefix="civictrack-privacy-tests-")
_OLD_TEMP_DIR = tempfile.tempdir
_ENV_KEYS = (
    "DEMO_MODE",
    "SECRET_KEY",
    "GEMINI_API_KEY",
    "CIVICTRACK_DATABASE_PATH",
    "CIVICTRACK_UPLOAD_FOLDER",
)
_OLD_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}
tempfile.tempdir = _TEMP_STORAGE.name
os.environ["DEMO_MODE"] = "true"
os.environ["SECRET_KEY"] = secrets.token_hex(32)
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("CIVICTRACK_DATABASE_PATH", None)
os.environ.pop("CIVICTRACK_UPLOAD_FOLDER", None)

import app as civic


def tearDownModule():
    for key, value in _OLD_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    tempfile.tempdir = _OLD_TEMP_DIR
    _TEMP_STORAGE.cleanup()


class GeminiPrivacyTests(unittest.TestCase):
    WATER_LEAK_DESCRIPTION = (
        "A continuous flow of water has been leaking from an underground water pipeline "
        "near the residential street for the past two days. A large amount of clean drinking "
        "water is being wasted, and water is accumulating on the road. The leakage is making "
        "it difficult for pedestrians and vehicles to pass safely. Please arrange an inspection "
        "and repair the damaged pipeline as soon as possible."
    )

    def test_ordinary_civic_phrases_are_sanitized_and_allowed(self):
        examples = (
            ("Garbage Overflow Near Residential Street", "residential area"),
            ("Garbage bins near the park are overflowing", "public area"),
            ("Garbage bins are overflowing", "Garbage bins"),
        )
        for description, expected_safe_phrase in examples:
            with self.subTest(description=description):
                sanitized = civic.sanitize_description_for_gemini(description)
                self.assertTrue(civic.description_is_safe_for_gemini(sanitized), sanitized)
                self.assertIn(expected_safe_phrase.casefold(), sanitized.casefold())
                self.assertNotRegex(
                    sanitized.casefold(),
                    r"\bnear\s+(?:(?:the|a|an)\s+)?(?:residential\s+street|(?:public\s+)?park)\b",
                )

    def test_water_leak_description_is_sanitized_before_gemini(self):
        original = self.WATER_LEAK_DESCRIPTION
        sanitized = civic.sanitize_description_for_gemini(original)
        self.assertTrue(civic.description_is_safe_for_gemini(sanitized), sanitized)
        self.assertNotEqual(sanitized, original)
        self.assertNotIn("near the residential street", sanitized.casefold())
        self.assertIn("in a residential area", sanitized.casefold())

        response = {
            "predicted_category": "Water Leakage",
            "suggested_severity": "Medium",
            "recommended_department": civic.DEPARTMENT_BY_CATEGORY["Water Leakage"],
            "short_summary": "A damaged pipeline is wasting water.",
            "reasoning": "The description reports a continuous water leak.",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "TEST_ONLY_KEY"}), patch.object(
            civic,
            "request_gemini_analysis",
            return_value=json.dumps(response),
        ) as request_gemini:
            result = civic.analyze_complaint(
                "Water Leakage", "Underground pipeline leak", original
            )

        self.assertEqual(result["analysis_source"], "Gemini AI")
        request_gemini.assert_called_once()
        sent_category, sent_description, _server_key = request_gemini.call_args.args
        self.assertEqual(sent_category, "Water Leakage")
        self.assertIn("in a residential area", sent_description.casefold())
        self.assertNotIn("near the residential street", sent_description.casefold())
        self.assertNotEqual(sent_description, original)

    def test_gemini_receives_only_category_and_sanitized_description(self):
        private_values = (
            "Asha Nair",
            "9876543210",
            "17 Palm Lane, Hyderabad",
            "Central Park",
        )
        response = {
            "predicted_category": "Garbage",
            "suggested_severity": "Medium",
            "recommended_department": civic.DEPARTMENT_BY_CATEGORY["Garbage"],
            "short_summary": "Garbage collection needs attention.",
            "reasoning": "The description reports overflowing bins.",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "TEST_ONLY_KEY"}), patch.object(
            civic,
            "request_gemini_analysis",
            return_value=json.dumps(response),
        ) as request_gemini:
            result = civic.analyze_complaint(
                "Garbage",
                "Reporter Asha Nair 9876543210",
                "Garbage bins near the park are overflowing",
                private_values=private_values,
            )

        self.assertEqual(result["analysis_source"], "Gemini AI")
        request_gemini.assert_called_once()
        category, sent_description, _server_key = request_gemini.call_args.args
        self.assertEqual(category, "Garbage")
        self.assertIn("public area", sent_description)
        for private_value in private_values:
            self.assertNotIn(private_value, sent_description)
        self.assertNotIn("Asha", sent_description)
        self.assertNotIn("9876543210", sent_description)
        self.assertNotIn("Palm Lane", sent_description)
        self.assertNotIn("Central Park", sent_description)

    def test_personal_or_precise_location_text_uses_fallback_without_request(self):
        sensitive_descriptions = (
            "Garbage overflow reported by Ramesh Kumar",
            "Garbage Overflow, Will noted, on the street",
            "Garbage overflow, call 9876543210",
            "Garbage overflow at 17 Palm Lane",
            "Garbage overflow near Jubilee Gardens",
            "Garbage overflow at coordinates 17.385, 78.4867",
        )
        for description in sensitive_descriptions:
            with self.subTest(description=description), patch.dict(
                os.environ, {"GEMINI_API_KEY": "TEST_ONLY_KEY"}
            ), patch.object(civic, "request_gemini_analysis") as request_gemini:
                result = civic.analyze_complaint("Garbage", "Overflow", description)
                self.assertEqual(result["analysis_source"], "Rule-based fallback")
                request_gemini.assert_not_called()

    def test_known_reporter_details_in_description_are_not_sent(self):
        private_values = (
            "Asha Nair",
            "9876543210",
            "17 Palm Lane, Hyderabad",
            "Central Park",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "TEST_ONLY_KEY"}), patch.object(
            civic, "request_gemini_analysis"
        ) as request_gemini:
            result = civic.analyze_complaint(
                "Garbage",
                "Overflow",
                "Garbage overflow reported by Asha Nair near Central Park at 17 Palm Lane.",
                private_values=private_values,
            )
        self.assertEqual(result["analysis_source"], "Rule-based fallback")
        request_gemini.assert_not_called()

    def test_form_contact_location_and_coordinates_stay_out_of_gemini_request(self):
        response = {
            "predicted_category": "Water Leakage",
            "suggested_severity": "Medium",
            "recommended_department": civic.DEPARTMENT_BY_CATEGORY["Water Leakage"],
            "short_summary": "A water leak needs inspection.",
            "reasoning": "The report describes a pipeline leak.",
        }
        client = civic.app.test_client()
        form_page = client.get("/report")
        token_match = re.search(rb'name="csrf_token" value="([^"]+)"', form_page.data)
        self.assertIsNotNone(token_match)
        form = {
            "csrf_token": token_match.group(1).decode(),
            "citizen_name": "Asha Nair",
            "phone_number": "9876543210",
            "contact_consent": "yes",
            "title": "Leaking pipeline at junction",
            "category": "Water Leakage",
            "description": self.WATER_LEAK_DESCRIPTION,
            "address": "17 Palm Lane, Hyderabad",
            "landmark": "Central Park",
            "location_method": "GPS",
            "latitude": "17.385000",
            "longitude": "78.486700",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "TEST_ONLY_KEY"}), patch.object(
            civic,
            "request_gemini_analysis",
            return_value=json.dumps(response),
        ) as request_gemini:
            submitted = client.post("/report", data=form)

        self.assertEqual(submitted.status_code, 302)
        request_gemini.assert_called_once()
        category, sent_description, _server_key = request_gemini.call_args.args
        self.assertEqual(category, "Water Leakage")
        for private_value in (
            "Asha Nair",
            "9876543210",
            "17 Palm Lane",
            "Central Park",
            "17.385000",
            "78.486700",
        ):
            self.assertNotIn(private_value, sent_description)
        self.assertIn("in a residential area", sent_description.casefold())


if __name__ == "__main__":
    unittest.main()
