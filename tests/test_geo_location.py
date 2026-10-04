import os
import sys
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app


class GeoLocationTests(unittest.TestCase):
    def setUp(self):
        with app.GEO_CACHE_LOCK:
            app.GEO_CACHE.clear()

    def test_ipapi_fallback_produces_valid_approximate_coordinates(self):
        denied = Mock(status_code=429)
        fallback = Mock(status_code=200)
        fallback.json.return_value = {
            "country_name": "India",
            "region": "Karnataka",
            "city": "Bengaluru",
            "latitude": 12.9716,
            "longitude": 77.5946,
            "org": "Example ISP",
            "asn": "AS12345",
        }
        with patch.object(app.requests, "get", side_effect=[denied, fallback]) as get:
            geo = app._geolocate_ip("8.8.8.8")

        self.assertEqual(get.call_count, 2)
        self.assertEqual(geo["geo_status"], "resolved")
        self.assertEqual(geo["geo_source"], "ipapi.co")
        self.assertEqual(app._geo_summary(geo), "Bengaluru, Karnataka, India")
        self.assertEqual((geo["lat"], geo["lon"]), (12.9716, 77.5946))

    def test_invalid_coordinates_are_not_plotted(self):
        primary = Mock(status_code=200)
        primary.json.return_value = {
            "status": "success",
            "country": "India",
            "regionName": "Karnataka",
            "city": "Bengaluru",
            "lat": 91,
            "lon": 181,
        }
        with patch.object(app.requests, "get", return_value=primary):
            geo = app._geolocate_ip("1.1.1.1")

        self.assertEqual(geo["geo_status"], "partial")
        self.assertIsNone(geo["lat"])
        self.assertIsNone(geo["lon"])
        self.assertEqual(app._geo_summary(geo), "Bengaluru, Karnataka, India")

    def test_non_public_ip_skips_external_lookup(self):
        with patch.object(app.requests, "get") as get:
            geo = app._geolocate_ip("127.0.0.1")

        get.assert_not_called()
        self.assertEqual(geo["geo_status"], "not_public_ip")

    def test_unavailable_provider_is_not_cached(self):
        with patch.object(app.requests, "get", side_effect=app.requests.ConnectionError):
            geo = app._geolocate_ip("8.8.4.4")

        self.assertEqual(geo["geo_status"], "unavailable")
        self.assertNotIn("8.8.4.4", app.GEO_CACHE)

    def test_canary_hit_saves_geo_status_and_source(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/receipt/TEST",
            "raw_path": b"/receipt/TEST",
            "query_string": b"",
            "headers": [
                (b"x-real-ip", b"8.8.8.8"),
                (b"user-agent", b"Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile"),
            ],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 45678),
        })
        geo = {
            "country": "India",
            "region": "Karnataka",
            "city": "Bengaluru",
            "lat": 12.9716,
            "lon": 77.5946,
            "geo_status": "resolved",
            "geo_source": "ipapi.co",
        }
        with app.STATE_LOCK:
            app.STATE["canary_hits"].clear()
            app.STATE["outbox_queue"].clear()
        try:
            with patch.object(app, "_geolocate_ip", return_value=geo):
                app.canary_trap_receipt("TEST", request)
            with app.STATE_LOCK:
                hit = app.STATE["canary_hits"][-1]
            self.assertEqual(hit["location"], "Bengaluru, Karnataka, India")
            self.assertEqual(hit["geo_status"], "resolved")
            self.assertEqual(hit["geo_source"], "ipapi.co")
        finally:
            with app.STATE_LOCK:
                app.STATE["canary_hits"].clear()
                app.STATE["outbox_queue"].clear()

    def test_receipt_page_discloses_location_and_requires_explicit_browser_consent(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/receipt/CONSENT-TEST",
            "raw_path": b"/receipt/CONSENT-TEST",
            "query_string": b"",
            "headers": [(b"x-real-ip", b"8.8.8.8")],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 45678),
        })
        geo = {
            "country": "India",
            "city": "Bengaluru",
            "lat": 12.9716,
            "lon": 77.5946,
            "geo_status": "resolved",
            "geo_source": "ipapi.co",
        }
        with app.STATE_LOCK:
            app.STATE["canary_hits"].clear()
            app.STATE["outbox_queue"].clear()
        try:
            with patch.object(app, "_geolocate_ip", return_value=geo):
                response = app.canary_trap_receipt("CONSENT-TEST", request)
            page = response.body.decode("utf-8")
            self.assertIn("ScamTrap AI security test", page)
            self.assertIn("Optional device-location sharing", page)
            self.assertIn("Share device location", page)
            self.assertIn("navigator.geolocation.getCurrentPosition", page)
            self.assertIn("share-location", page)
            self.assertIn("public IP address, browser, and an approximate IP-based location", page)
            self.assertNotIn("STATE BANK OF INDIA", page)
            self.assertNotIn("alternate UPI", page)
        finally:
            with app.STATE_LOCK:
                app.STATE["canary_hits"].clear()
                app.STATE["outbox_queue"].clear()

    def test_shared_coordinates_require_matching_receipt_and_ip(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/receipt/CONSENT-TEST/share-location",
            "raw_path": b"/receipt/CONSENT-TEST/share-location",
            "query_string": b"",
            "headers": [(b"x-real-ip", b"8.8.8.8")],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 45678),
        })
        hit = {
            "receipt_id": "CONSENT-TEST",
            "ip": "8.8.8.8",
            "lat": 12.9,
            "lon": 77.5,
            "location": "Approximate IP location",
        }
        with app.STATE_LOCK:
            app.STATE["canary_hits"].clear()
            app.STATE["canary_hits"].append(hit)
        try:
            response = app.share_canary_location(
                "CONSENT-TEST",
                request,
                {"latitude": 12.9716, "longitude": 77.5946, "accuracy": 23.7},
            )
            self.assertEqual(response["status"], "ok")
            self.assertEqual((hit["lat"], hit["lon"]), (12.9716, 77.5946))
            self.assertEqual(hit["location_method"], "browser_geolocation_consent")
            self.assertEqual(hit["location_accuracy_m"], 23.7)
            self.assertEqual((hit["ip_lat"], hit["ip_lon"]), (12.9, 77.5))

            wrong_ip = Request({
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/receipt/CONSENT-TEST/share-location",
                "raw_path": b"/receipt/CONSENT-TEST/share-location",
                "query_string": b"",
                "headers": [(b"x-real-ip", b"1.1.1.1")],
                "server": ("127.0.0.1", 8000),
                "client": ("127.0.0.1", 45678),
            })
            mismatch = app.share_canary_location(
                "CONSENT-TEST",
                wrong_ip,
                {"latitude": 12.0, "longitude": 77.0, "accuracy": 10},
            )
            self.assertEqual(mismatch.status_code, 404)
        finally:
            with app.STATE_LOCK:
                app.STATE["canary_hits"].clear()

    def test_shared_coordinates_reject_invalid_values(self):
        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/receipt/CONSENT-TEST/share-location",
            "raw_path": b"/receipt/CONSENT-TEST/share-location",
            "query_string": b"",
            "headers": [(b"x-real-ip", b"8.8.8.8")],
            "server": ("127.0.0.1", 8000),
            "client": ("127.0.0.1", 45678),
        })
        invalid_payloads = [
            {"latitude": 91, "longitude": 77, "accuracy": 10},
            {"latitude": 12, "longitude": float("nan"), "accuracy": 10},
            {"latitude": 12, "longitude": 77, "accuracy": -1},
            {"latitude": 12, "longitude": 77, "accuracy": float("inf")},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                result = app.share_canary_location("CONSENT-TEST", request, payload)
                self.assertEqual(result.status_code, 400)

    def test_dashboard_uses_working_basemap_and_explains_ip_accuracy(self):
        page = app.get_soc_dashboard().body.decode("utf-8")

        self.assertIn("World_Street_Map/MapServer/tile", page)
        self.assertNotIn("basemaps.cartocdn.com", page)
        self.assertIn("This is not device GPS.", page)
        self.assertIn("IP-location lookup failed; check backend internet access/provider limits.", page)
        self.assertIn("state.scammer_chat || []).length", page)
        self.assertNotIn("String(all.length)", page)
        self.assertIn("maxZoom: 10", page)
        self.assertIn("VISITOR-SHARED DEVICE LOCATION", page)
        self.assertIn("Approximate IP-based estimate · not device GPS", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
