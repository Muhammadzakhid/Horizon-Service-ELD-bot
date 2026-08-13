"""Webhook va ALERT formatlari uchun avtomatik testlar."""

import unittest
from unittest.mock import patch

import app


class AlertBotTests(unittest.TestCase):
    def setUp(self):
        self.client = app.flask_app.test_client()
        app._recent_events.clear()

    def test_all_scenarios_have_required_fields(self):
        expected_titles = {
            "weigh_station": "⚖️ <b>WEIGH STATION NEARBY ALERT (20 Miles Left)</b>",
            "log_frozen": "❄️ <b>LOGBOOK FROZEN ALERT</b>",
            "driver_disconnected": "🔌 <b>DRIVER DISCONNECTED ALERT</b>",
        }
        required_labels = ("Company:", "Driver:", "Truck Unit:", "Location:", "Time:")
        for scenario, title in expected_titles.items():
            with self.subTest(scenario=scenario):
                message = app.SCENARIO_FORMATTERS[scenario]({})
                self.assertIn(title, message)
                for label in required_labels:
                    self.assertIn(label, message)
                self.assertEqual(message.count("N/A"), 5)

    def test_html_from_webhook_is_escaped(self):
        message = app.format_weigh_station({"driver_name": "<Admin & Driver>"})
        self.assertIn("&lt;Admin &amp; Driver&gt;", message)

    @patch("app.broadcast_alert", return_value={"sent": 2, "failed": 0, "total_subscribers": 2})
    def test_valid_webhook(self, broadcast):
        response = self.client.post(
            "/webhook/alert",
            json={"scenario": "driver_disconnected", "company_name": "Atlas LLC"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["sent"], 2)
        self.assertIn("Atlas LLC", broadcast.call_args.args[0])

    def test_unknown_scenario(self):
        response = self.client.post("/webhook/alert", json={"scenario": "other"})
        self.assertEqual(response.status_code, 400)

    @patch("app.broadcast_alert", return_value={"sent": 1, "failed": 0, "total_subscribers": 1})
    def test_7sky_endpoint_forces_company_name(self, broadcast):
        response = self.client.post(
            "/webhook/7sky",
            json={"scenario": "weigh_station", "company_name": "Wrong Company"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["company"], "7SKY LOGISTICS INC")
        message = broadcast.call_args.args[0]
        self.assertIn("Company: 7SKY LOGISTICS INC", message)
        self.assertNotIn("Wrong Company", message)

    @patch("app.broadcast_alert", return_value={"sent": 1, "failed": 0, "total_subscribers": 1})
    def test_msv_endpoint_forces_company_name(self, broadcast):
        response = self.client.post(
            "/webhook/msv",
            json={"scenario": "log_frozen"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["company"], "MSV TRANSPORT LLC")
        self.assertIn("Company: MSV TRANSPORT LLC", broadcast.call_args.args[0])

    def test_non_object_json_is_rejected(self):
        response = self.client.post("/webhook/alert", json=["weigh_station"])
        self.assertEqual(response.status_code, 400)

    @patch("app.broadcast_alert", return_value={"sent": 1, "failed": 0, "total_subscribers": 1})
    def test_nested_camelcase_payload_is_normalized(self, broadcast):
        response = self.client.post("/webhook/7sky/alert", json={"data": {
            "eventType": "driver-disconnected",
            "driverName": "John Driver",
            "unitNumber": "701",
            "currentLocation": "Dallas, TX",
            "eventTime": "2026-08-14 09:00 CDT",
        }})
        self.assertEqual(response.status_code, 200)
        message = broadcast.call_args.args[0]
        self.assertIn("John Driver", message)
        self.assertIn("Truck Unit: 701", message)

    @patch("app.broadcast_alert", return_value={"sent": 1, "failed": 0, "total_subscribers": 1})
    def test_duplicate_event_id_is_sent_only_once(self, broadcast):
        payload = {"eventType": "log_frozen", "eventId": "evt-100"}
        first = self.client.post("/webhook/msv", json=payload)
        second = self.client.post("/webhook/msv", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.get_json()["status"], "duplicate_ignored")
        self.assertEqual(broadcast.call_count, 1)

    @patch("app.broadcast_alert", return_value={"sent": 0, "failed": 0, "total_subscribers": 0})
    def test_no_subscribers_is_not_reported_as_sent(self, broadcast):
        response = self.client.post("/webhook/msv", json={"scenario": "log_frozen"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "no_subscribers")


if __name__ == "__main__":
    unittest.main()
