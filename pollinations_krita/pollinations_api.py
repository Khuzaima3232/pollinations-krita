"""Pollinations transport and BYOP device flow for Krita.

Python standard library only: Krita bundles its own interpreter, so pip
packages cannot be assumed and response bodies are never shown to the user
(an error body can echo the bearer credential).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

ENTER = "https://enter.pollinations.ai"
GEN = "https://gen.pollinations.ai"
USER_AGENT = "Pollinations-Krita/1.0"
TIMEOUT = 30


class ApiError(Exception):
    """Anything the user should see as a sentence, not a traceback."""


class Cancelled(ApiError):
    pass


def request_json(url, payload=None, token=None, timeout=TIMEOUT):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Read and discard: response bodies are never displayed.
        try:
            body = error.read()
        finally:
            error.close()
        if "/api/device/" in url:
            try:
                parsed = json.loads(body)
                if parsed.get("error") in {
                    "authorization_pending",
                    "slow_down",
                    "access_denied",
                    "expired_token",
                }:
                    return parsed
            except (ValueError, AttributeError):
                pass
        if error.code == 401:
            raise ApiError(
                "Authorization expired or was revoked. Connect your account again."
            ) from None
        if error.code == 402:
            raise ApiError(
                "Insufficient Pollen or budget. Add Pollen or raise the budget in your account."
            ) from None
        if error.code == 403:
            raise ApiError(
                "Access denied. Check the model permissions on your Pollinations account."
            ) from None
        if error.code == 429:
            raise ApiError("Rate limit reached. Wait a little and try again.") from None
        raise ApiError(
            "Pollinations returned HTTP %d. Check the request and try again later."
            % error.code
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError(
            "Connection failed or timed out. Check your network, and check account "
            "activity before retrying a generation."
        ) from None
    except (ValueError, UnicodeError):
        raise ApiError("Pollinations returned an invalid response. Try again later.") from None


def begin_authorization(app_key=None):
    """Step 1 of the device flow."""
    if app_key and not app_key.startswith("pk_"):
        raise ApiError("The app key must be a publishable pk_ key.")
    code = request_json(ENTER + "/api/device/code", {"client_id": app_key} if app_key else {})
    required = ("device_code", "user_code", "verification_uri", "expires_in")
    if not isinstance(code, dict) or not all(code.get(k) for k in required):
        raise ApiError("Invalid device authorization response.")
    code["deadline"] = time.monotonic() + float(code["expires_in"])
    url = urllib.parse.urljoin(
        ENTER, code.get("verification_uri_complete") or code["verification_uri"]
    )
    if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(ENTER)[:2]:
        raise ApiError("Unexpected authorization site.")
    code["approval_url"] = url
    return code


def poll_authorization(code, cancel):
    """Step 2: poll until approved. `cancel` is a threading.Event."""
    interval = max(5, float(code.get("interval", 5)))
    while time.monotonic() < code["deadline"]:
        if cancel.wait(min(interval, max(0, code["deadline"] - time.monotonic()))):
            raise Cancelled("Connection cancelled.")
        if time.monotonic() >= code["deadline"]:
            break
        result = request_json(ENTER + "/api/device/token", {"device_code": code["device_code"]})
        if cancel.is_set():
            raise Cancelled("Connection cancelled.")
        token = result.get("access_token")
        if isinstance(token, str) and token.startswith("sk_"):
            return token
        error = result.get("error")
        if error == "slow_down":
            interval += 5
        elif error == "access_denied":
            raise ApiError("Authorization declined. Connect again when ready.")
        elif error == "expired_token":
            break
        elif error != "authorization_pending":
            raise ApiError("Authorization failed. Connect again.")
    raise ApiError("Authorization code expired. Connect again for a new code.")


def load_image_models(token):
    """Image models the account may use."""
    catalog = request_json(GEN + "/image/models", token=token)
    if not isinstance(catalog, list):
        raise ApiError("Invalid model catalog.")
    models = [
        m
        for m in catalog
        if isinstance(m, dict)
        and isinstance(m.get("name"), str)
        and "image" in m.get("output_modalities", [])
        and "video" not in m.get("output_modalities", [])
    ]
    if not models:
        raise ApiError(
            "No image models available. Check your account's model permissions and balance."
        )
    return models


def supports_image_input(model):
    return "image" in model.get("input_modalities", [])


def generate(token, model, prompt, width=None, height=None, source=None):
    """Generate an image, or edit `source` bytes, returning PNG bytes."""
    import base64

    if not prompt.strip():
        raise ApiError("Enter a prompt.")
    payload = {"model": model["name"], "prompt": prompt, "response_format": "b64_json"}
    if width and height:
        payload["width"] = int(width)
        payload["height"] = int(height)
    route = "/v1/images/generations"
    if source is not None:
        if not supports_image_input(model):
            raise ApiError("Choose a model that accepts image input to edit a picture.")
        payload["image"] = "data:image/png;base64," + base64.b64encode(source).decode("ascii")
        route = "/v1/images/edits"
    # Generations may legitimately outlive a fixed deadline; keep waiting.
    result = request_json(GEN + route, payload, token, timeout=None)
    try:
        encoded = result["data"][0]["b64_json"]
        data = base64.b64decode(encoded, validate=True)
        if not data:
            raise ValueError()
        return data
    except (KeyError, IndexError, TypeError, ValueError):
        raise ApiError(
            "Generation returned no image. Check your account activity before retrying."
        ) from None
