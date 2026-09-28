"""Tests for the Krita plug-in's transport layer.

The API module is standard library only, so it is tested directly against a real
localhost HTTP server: requests genuinely go over the wire and responses are
genuinely parsed, but no external credentials are used and no Pollen is spent.

    python -m unittest -v test_api

The module is loaded by path rather than by package import: the package's
__init__ imports Krita and Qt, which only exist inside Krita, and the transport
does not need either.
"""
import importlib.util
import json
import pathlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

_API_PATH = pathlib.Path(__file__).with_name("pollinations_krita") / "pollinations_api.py"
_spec = importlib.util.spec_from_file_location("pollinations_api", _API_PATH)
assert _spec and _spec.loader
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)

RECORDED = []
RESPONDER = {"fn": lambda path, body: (200, {})}


class StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        RECORDED.append(
            {
                "method": method,
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        status, payload = RESPONDER["fn"](self.path, body)
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = "http://127.0.0.1:%d" % cls.server.server_port
        # Point the module at the stub server.
        cls._enter, cls._gen = api.ENTER, api.GEN
        api.ENTER = cls.origin
        api.GEN = cls.origin

    @classmethod
    def tearDownClass(cls):
        api.ENTER, api.GEN = cls._enter, cls._gen
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        RECORDED.clear()
        RESPONDER["fn"] = lambda path, body: (200, {})

    def code_payload(self):
        return {
            "device_code": "dev-abc",
            "user_code": "NRX8V2H8",
            "verification_uri": api.ENTER + "/device",
            "verification_uri_complete": api.ENTER + "/device?user_code=NRX8V2H8",
            "expires_in": 1800,
            "interval": 5,
        }

    def test_begin_authorization_returns_the_code_and_an_approval_url(self):
        RESPONDER["fn"] = lambda path, body: (200, self.code_payload())
        code = api.begin_authorization()
        self.assertEqual(code["user_code"], "NRX8V2H8")
        self.assertIn("user_code=NRX8V2H8", code["approval_url"])
        self.assertGreater(code["deadline"], 0)
        self.assertEqual(RECORDED[0]["method"], "POST")
        self.assertTrue(RECORDED[0]["path"].endswith("/api/device/code"))

    def test_begin_authorization_rejects_a_foreign_approval_site(self):
        payload = self.code_payload()
        payload["verification_uri"] = "https://evil.example.com/device"
        payload.pop("verification_uri_complete")
        RESPONDER["fn"] = lambda path, body: (200, payload)
        with self.assertRaises(api.ApiError) as caught:
            api.begin_authorization()
        self.assertIn("Unexpected authorization site", str(caught.exception))

    def test_begin_authorization_rejects_an_incomplete_response(self):
        RESPONDER["fn"] = lambda path, body: (200, {"device_code": "d"})
        with self.assertRaises(api.ApiError):
            api.begin_authorization()

    def test_begin_authorization_rejects_a_non_pk_app_key(self):
        with self.assertRaises(api.ApiError) as caught:
            api.begin_authorization("sk_not_publishable")
        self.assertIn("pk_", str(caught.exception))

    def test_poll_authorization_waits_through_pending_then_returns_the_token(self):
        calls = {"n": 0}

        def responder(path, body):
            if path.endswith("/api/device/code"):
                return (200, self.code_payload() | {"interval": 0})
            calls["n"] += 1
            if calls["n"] < 3:
                return (200, {"error": "authorization_pending"})
            return (200, {"access_token": "sk_live_token"})

        RESPONDER["fn"] = responder
        code = api.begin_authorization()
        token = api.poll_authorization(code, threading.Event())
        self.assertEqual(token, "sk_live_token")
        self.assertEqual(calls["n"], 3)

    def test_poll_authorization_widens_the_interval_on_slow_down(self):
        calls = {"n": 0}
        intervals = []

        def responder(path, body):
            calls["n"] += 1
            if calls["n"] == 1:
                return (200, {"error": "slow_down"})
            intervals.append(True)
            return (200, {"access_token": "sk_after_slowdown"})

        RESPONDER["fn"] = responder
        code = {"device_code": "d", "deadline": float("inf"), "interval": 0}
        token = api.poll_authorization(code, threading.Event())
        self.assertEqual(token, "sk_after_slowdown")

    def test_poll_authorization_reports_a_declined_request(self):
        RESPONDER["fn"] = lambda path, body: (200, {"error": "access_denied"})
        code = {"device_code": "d", "deadline": float("inf"), "interval": 0}
        with self.assertRaises(api.ApiError) as caught:
            api.poll_authorization(code, threading.Event())
        self.assertIn("declined", str(caught.exception))

    def test_poll_authorization_stops_when_cancelled(self):
        RESPONDER["fn"] = lambda path, body: (200, {"error": "authorization_pending"})
        cancel = threading.Event()
        cancel.set()
        code = {"device_code": "d", "deadline": float("inf"), "interval": 0}
        with self.assertRaises(api.Cancelled):
            api.poll_authorization(code, cancel)

    def test_http_errors_become_sentences(self):
        expectations = {
            401: "expired or was revoked",
            402: "Insufficient Pollen",
            403: "Access denied",
            429: "Rate limit",
            500: "HTTP 500",
        }
        for status, fragment in expectations.items():
            RESPONDER["fn"] = lambda path, body, s=status: (s, {"error": "boom"})
            with self.assertRaises(api.ApiError) as caught:
                api.load_image_models("sk_x")
            self.assertIn(fragment, str(caught.exception))

    def test_the_response_body_never_reaches_the_message(self):
        RESPONDER["fn"] = lambda path, body: (402, {"echo": "sk_leaked_token_here"})
        with self.assertRaises(api.ApiError) as caught:
            api.load_image_models("sk_x")
        self.assertNotIn("sk_leaked_token_here", str(caught.exception))

    def test_load_image_models_filters_out_video_models(self):
        RESPONDER["fn"] = lambda path, body: (
            200,
            [
                {"name": "a/image", "output_modalities": ["image"]},
                {"name": "b/video", "output_modalities": ["video"]},
                {
                    "name": "c/editor",
                    "output_modalities": ["image"],
                    "input_modalities": ["image", "text"],
                },
            ],
        )
        models = api.load_image_models("sk_x")
        self.assertEqual([m["name"] for m in models], ["a/image", "c/editor"])
        self.assertTrue(api.supports_image_input(models[1]))
        self.assertFalse(api.supports_image_input(models[0]))

    def test_load_image_models_fails_when_none_are_usable(self):
        RESPONDER["fn"] = lambda path, body: (200, [{"name": "x", "output_modalities": ["video"]}])
        with self.assertRaises(api.ApiError) as caught:
            api.load_image_models("sk_x")
        self.assertIn("No image models available", str(caught.exception))

    def test_generate_asks_for_base64_and_decodes_it(self):
        import base64

        raw = b"\x89PNG\r\n"
        RESPONDER["fn"] = lambda path, body: (
            200,
            {"data": [{"b64_json": base64.b64encode(raw).decode("ascii")}]},
        )
        model = {"name": "m", "output_modalities": ["image"], "input_modalities": ["image"]}
        result = api.generate("sk_x", model, "a fox", 512, 512)
        self.assertEqual(result, raw)
        sent = json.loads(RECORDED[-1]["body"])
        self.assertEqual(sent["response_format"], "b64_json")
        self.assertEqual(sent["width"], 512)
        self.assertTrue(RECORDED[-1]["path"].endswith("/v1/images/generations"))
        self.assertEqual(RECORDED[-1]["auth"], "Bearer sk_x")

    def test_generate_edits_through_the_edits_route_with_a_data_url(self):
        RESPONDER["fn"] = lambda path, body: (200, {"data": [{"b64_json": "AAAA"}]})
        model = {"name": "m", "output_modalities": ["image"], "input_modalities": ["image"]}
        api.generate("sk_x", model, "make it gold", None, None, source=b"\x01\x02")
        sent = json.loads(RECORDED[-1]["body"])
        self.assertTrue(RECORDED[-1]["path"].endswith("/v1/images/edits"))
        self.assertTrue(sent["image"].startswith("data:image/png;base64,"))

    def test_generate_refuses_to_edit_with_a_model_without_image_input(self):
        model = {"name": "m", "output_modalities": ["image"], "input_modalities": ["text"]}
        with self.assertRaises(api.ApiError) as caught:
            api.generate("sk_x", model, "x", None, None, source=b"\x01")
        self.assertIn("accepts image input", str(caught.exception))

    def test_generate_refuses_an_empty_prompt(self):
        model = {"name": "m", "output_modalities": ["image"], "input_modalities": []}
        with self.assertRaises(api.ApiError) as caught:
            api.generate("sk_x", model, "   ")
        self.assertIn("Enter a prompt", str(caught.exception))

    def test_generate_reports_a_response_with_no_image(self):
        RESPONDER["fn"] = lambda path, body: (200, {"data": []})
        model = {"name": "m", "output_modalities": ["image"], "input_modalities": []}
        with self.assertRaises(api.ApiError) as caught:
            api.generate("sk_x", model, "x")
        self.assertIn("returned no image", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
