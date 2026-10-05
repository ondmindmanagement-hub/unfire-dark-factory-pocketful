"""HTTP layer: parsing, authentication, idempotency headers and routing.

Domain rules live in wallet.py; this module only translates HTTP to calls on the
Store and errors to the `{"error": {"code", "message"}}` envelope.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from wallet import ApiError, Store, validation

STORE = Store()
MAX_KEY = 255
DIGITS = re.compile(r"^[0-9]+$")
REQUEST_ACTION = re.compile(r"^/requests/([^/]+)/(pay|decline|cancel)$")
AUTHORIZATION_ACTION = re.compile(r"^/authorizations/([^/]+)/(capture|void)$")
PAYMENT_ACTION = re.compile(r"^/payments/([^/]+)/(corrections|revisions|refunds)$")

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
UI_ROUTES = ("/", "/requests", "/split", "/signup", "/login", "/authorizations")
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml"}


def load_static() -> dict:
    files = {}
    for name in os.listdir(UI_DIR):
        with open(os.path.join(UI_DIR, name), "rb") as fh:
            files[name] = fh.read()
    return files


STATIC = load_static()


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal,
                          parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError(400, "malformed_request", "body is not valid JSON") from None


def int_param(query: dict, name: str, default: int, minimum: int, maximum: int | None) -> int:
    values = query.get(name)
    if not values:
        return default
    raw = values[0]
    if not DIGITS.match(raw):
        raise validation(f"{name} must be written as plain decimal digits")
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        raise validation(f"{name} is out of range")
    return value


def enum_param(query: dict, name: str, allowed: tuple):
    values = query.get(name)
    if not values:
        return None
    if values[0] not in allowed:
        raise validation(f"{name} must be one of {', '.join(allowed)}")
    return values[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wallet-service"

    def log_message(self, *args):
        pass

    # ---------------------------------------------------------- plumbing
    def send_json(self, status: int, payload=None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def wants_html(self) -> bool:
        return "text/html" in (self.headers.get("Accept") or "").lower()

    def send_error_body(self, err: ApiError) -> None:
        self.send_json(err.status, {"error": {"code": err.code, "message": err.message}})

    def raw_body(self) -> bytes:
        if getattr(self, "_raw", None) is None:
            length = int(self.headers.get("Content-Length") or 0)
            self._raw = self.rfile.read(length) if length > 0 else b""
        return self._raw

    def json_object(self, *, empty_ok: bool = False) -> dict:
        raw = self.raw_body()
        if not raw.strip():
            if empty_ok:
                return {}
            raise ApiError(400, "malformed_request", "a JSON object body is required")
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            raise ApiError(400, "malformed_request", "body must be a JSON object")
        return parsed

    def current_user(self) -> dict:
        header = self.headers.get("Authorization") or ""
        parts = header.split()
        token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None
        return STORE.authenticate(token)

    def idempotency_key(self) -> str:
        key = self.headers.get("Idempotency-Key")
        if key is None or key == "":
            raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header is required")
        if len(key) > MAX_KEY:
            raise validation(f"Idempotency-Key must be 1 to {MAX_KEY} characters")
        return key

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        self._raw = None
        try:
            split = urlsplit(self.path)
            path = unquote(split.path).rstrip("/") or "/"
            query = parse_qs(split.query, keep_blank_values=True)
            self.route(method, path, query)
        except ApiError as err:
            self.send_error_body(err)
        except Exception:  # never leak a 5xx without the envelope
            traceback.print_exc(file=sys.stderr)
            self.send_error_body(ApiError(500, "internal_error", "unexpected error"))
        finally:
            # Drain any unread body so keep-alive connections stay in sync.
            try:
                self.raw_body()
            except Exception:
                self.close_connection = True

    # ------------------------------------------------------------ routes
    def route(self, method: str, path: str, query: dict) -> None:
        # Browser screens. Pure UI routes always render; routes shared with the
        # API render only for Accept: text/html and otherwise fall through to JSON.
        if method == "GET" and path in UI_ROUTES:
            if path in ("/", "/split", "/signup", "/login") or self.wants_html():
                return self.send_bytes(200, STATIC["index.html"], "text/html; charset=utf-8")
        if method == "GET" and path.startswith("/static/"):
            name = path[len("/static/"):]
            ext = os.path.splitext(name)[1]
            if name in STATIC and ext in STATIC_TYPES:
                return self.send_bytes(200, STATIC[name], STATIC_TYPES[ext])
            raise ApiError(404, "not_found", "no such asset")
        routes = {
            "/health": {"GET": self.health},
            "/_test/reset": {"POST": self.reset},
            "/_test/export": {"GET": self.export},
            "/_test/import": {"POST": self.import_},
            "/auth/signup": {"POST": self.signup},
            "/auth/login": {"POST": self.login},
            "/me": {"GET": self.me},
            "/statement": {"GET": self.statement},
            "/payments": {"POST": self.create_payment},
            "/activity": {"GET": self.activity},
            "/requests": {"GET": self.list_requests, "POST": self.create_request},
            "/splits": {"POST": self.create_split},
            "/settlements": {"POST": self.create_settlement},
            "/correction-batches": {"POST": self.create_correction_batch},
            "/authorizations": {"GET": self.list_authorizations,
                                "POST": self.create_authorization},
        }
        handlers = routes.get(path)
        args: tuple = (query,)
        if handlers is None:
            match = REQUEST_ACTION.match(path)
            if match:
                request_id, action = match.groups()
                handlers = {"POST": getattr(self, f"{action}_request")}
                args = (request_id,)
        if handlers is None:
            match = AUTHORIZATION_ACTION.match(path)
            if match:
                authorization_id, action = match.groups()
                handlers = {"POST": getattr(self, f"{action}_authorization")}
                args = (authorization_id,)
        if handlers is None:
            match = PAYMENT_ACTION.match(path)
            if match:
                payment_id, action = match.groups()
                handlers = {"GET" if action == "revisions" else "POST": getattr(self, action)}
                args = (payment_id,)
        if handlers is None:
            raise ApiError(404, "not_found", "no such endpoint")
        handler = handlers.get(method)
        if handler is None:
            raise ApiError(405, "method_not_allowed", "method not allowed")
        handler(*args)

    # test control
    def health(self, _query):
        self.send_json(200, {"status": "ok"})

    def reset(self, _query):
        STORE.reset(self.json_object())
        self.send_json(204)

    def export(self, _query):
        self.send_json(200, STORE.export())

    def import_(self, _query):
        STORE.import_(self.json_object())
        self.send_json(204)

    # auth
    def signup(self, _query):
        self.send_json(201, STORE.signup(self.json_object()))

    def login(self, _query):
        self.send_json(200, STORE.login(self.json_object()))

    # reads
    def me(self, query):
        self.send_json(200, STORE.me(self.current_user(), query))

    def statement(self, query):
        user = self.current_user()
        limit = int_param(query, "limit", 50, 1, 200)
        offset = int_param(query, "offset", 0, 0, None)
        self.send_json(200, STORE.statement(user, query, limit, offset))

    def revisions(self, payment_id):
        self.send_json(200, STORE.revisions(self.current_user(), payment_id))

    def corrections(self, payment_id):
        self._idempotent(f"/payments/{payment_id}/corrections",
                         lambda user, body: STORE.correct_payment(user, payment_id, body))

    def refunds(self, payment_id):
        self._idempotent(f"/payments/{payment_id}/refunds",
                         lambda user, body: STORE.refund_payment(user, payment_id, body))

    def activity(self, query):
        user = self.current_user()
        limit = int_param(query, "limit", 50, 1, 200)
        offset = int_param(query, "offset", 0, 0, None)
        self.send_json(200, STORE.activity(user, limit, offset))

    def list_requests(self, query):
        user = self.current_user()
        direction = enum_param(query, "direction", ("incoming", "outgoing"))
        status = enum_param(query, "status", ("pending", "paid", "declined", "cancelled"))
        limit = int_param(query, "limit", 50, 1, 200)
        offset = int_param(query, "offset", 0, 0, None)
        self.send_json(200, STORE.list_requests(user, direction, status, limit, offset))

    # idempotent writes
    def _idempotent(self, path: str, action, *, empty_ok: bool = False, user=None) -> None:
        user = user or self.current_user()
        body = self.json_object(empty_ok=empty_ok)
        key = self.idempotency_key()
        status, payload = STORE.idempotent(user["id"], "POST", path, key, body,
                                           lambda: action(user, body))
        self.send_json(status, payload)

    def create_payment(self, _query):
        self._idempotent("/payments", STORE.create_payment)

    def create_request(self, _query):
        self._idempotent("/requests", STORE.create_request)

    def pay_request(self, request_id):
        self._idempotent(f"/requests/{request_id}/pay",
                         lambda user, body: STORE.pay_request(user, request_id, body),
                         empty_ok=True)

    def create_split(self, _query):
        self._idempotent("/splits", STORE.create_split)

    def create_settlement(self, _query):
        user = self.current_user()
        if not STORE.is_operator(user["id"]):
            raise ApiError(403, "forbidden", "settlements require an operator")
        self._idempotent("/settlements", STORE.create_settlement, user=user)

    def create_correction_batch(self, _query):
        user = self.current_user()
        if not STORE.is_operator(user["id"]):
            raise ApiError(403, "forbidden", "correction batches require an operator")
        self._idempotent("/correction-batches", STORE.create_correction_batch, user=user)

    # non-idempotent request transitions
    def decline_request(self, request_id):
        self.send_json(200, STORE.resolve_request(self.current_user(), request_id, "declined"))

    def cancel_request(self, request_id):
        self.send_json(200, STORE.resolve_request(self.current_user(), request_id, "cancelled"))

    # authorizations and captures
    def list_authorizations(self, query):
        user = self.current_user()
        direction = enum_param(query, "direction", ("incoming", "outgoing"))
        status = enum_param(query, "status", ("open", "captured", "voided", "expired"))
        limit = int_param(query, "limit", 50, 1, 200)
        offset = int_param(query, "offset", 0, 0, None)
        self.send_json(200, STORE.list_authorizations(user, direction, status, limit, offset))

    def create_authorization(self, _query):
        self._idempotent("/authorizations", STORE.create_authorization)

    def capture_authorization(self, authorization_id):
        self._idempotent(f"/authorizations/{authorization_id}/capture",
                         lambda user, body: STORE.capture_authorization(user, authorization_id, body),
                         empty_ok=True)

    def void_authorization(self, authorization_id):
        self.send_json(200, STORE.void_authorization(self.current_user(), authorization_id))


class Server(ThreadingHTTPServer):
    request_queue_size = 512
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"listening on 0.0.0.0:{port}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
