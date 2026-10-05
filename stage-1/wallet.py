"""Domain core: wallets, payments, requests, splits, settlements, idempotency.

All state lives in one plain-JSON dict guarded by a single re-entrant lock. Every
read-modify-write of money happens inside that lock, so:

* balances can never be observed mid-transfer (debit and credit are one step);
* no balance can go negative, even transiently (checks and writes are atomic);
* an idempotency key is claimed and its effect applied in the same critical
  section, so concurrent duplicates collapse to exactly one effect.

Slow work (password hashing) is always done outside the lock.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal

from passwords import hash_password, looks_hashed, verify_password

TRACK = "pocketful"
FORMAT_VERSION = 1

HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MAX_AMOUNT = 1_000_000_000
MAX_NOTE = 200
MAX_ID = 64
VISIBILITIES = ("public", "private")
REQUEST_STATUSES = ("pending", "paid", "declined", "cancelled")
MINOR_UNITS = (0, 2, 3)
MAX_TRANSFERS = 32


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


def validation(message: str) -> ApiError:
    return ApiError(422, "validation_failed", message)


# ---------------------------------------------------------------- helpers

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value) -> int:
    """Epoch microseconds of an RFC 3339 timestamp with an explicit offset."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp needs an offset")
    return round(dt.timestamp() * 1_000_000)


def is_number(value) -> bool:
    return isinstance(value, (int, Decimal, float)) and not isinstance(value, bool)


def integral(value):
    """Return the exact int for an integral JSON number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        return int(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or not value.is_integer():
            return None
        return int(value)
    return None


def canonical(value) -> str:
    """Canonical JSON text: equal JSON values give equal text."""
    def norm(v):
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items()}
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return v
        if isinstance(v, (int, Decimal, float)):
            as_int = integral(v)
            if as_int is not None:
                return as_int
            return float(v)
        return v
    return json.dumps(norm(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def amount_field(body: dict, field: str = "amount") -> int:
    if field not in body:
        raise validation(f"{field} is required")
    value = integral(body[field]) if is_number(body[field]) else None
    if value is None:
        raise validation(f"{field} must be an integer number of minor units")
    if value < 1 or value > MAX_AMOUNT:
        raise validation(f"{field} must be between 1 and {MAX_AMOUNT}")
    return value


def note_field(body: dict) -> str:
    if "note" not in body:
        return ""
    note = body["note"]
    if not isinstance(note, str):
        raise validation("note must be a string")
    if len(note) > MAX_NOTE:
        raise validation(f"note must be at most {MAX_NOTE} characters")
    return note


def visibility_field(body: dict) -> str:
    if "visibility" not in body:
        return "public"
    vis = body["visibility"]
    if not isinstance(vis, str) or vis not in VISIBILITIES:
        raise validation("visibility must be public or private")
    return vis


def handle_field(body: dict, field: str, wrong_type_status: int = 400) -> str:
    if field not in body:
        raise validation(f"{field} is required")
    value = body[field]
    if not isinstance(value, str):
        if wrong_type_status == 400:
            raise ApiError(400, "malformed_request", f"{field} must be a string")
        raise validation(f"{field} must be a string")
    if not HANDLE_RE.match(value):
        raise validation(f"{field} is not a valid handle")
    return value


def derive_handle(email: str) -> str:
    local = email.rsplit("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", local)[:20]


def public(record: dict) -> dict:
    return {k: v for k, v in record.items() if not k.startswith("_")}


def empty_state() -> dict:
    return {
        "currency": "EUR",
        "minor_units": 2,
        "users": {},
        "tokens": {},
        "payments": {},
        "requests": {},
        "splits": {},
        "settlements": {},
        "operators": [],
        "idempotency": {},
        "seq": 0,
        "counters": {},
    }


# ------------------------------------------------------------------ store

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._install(empty_state())

    # ---- internal state management ----
    def _install(self, state: dict) -> None:
        """Swap in a fully-built state and rebuild indexes. Caller holds the lock
        (or is the constructor)."""
        self.state = state
        self.by_email = {u["email"].lower(): uid for uid, u in state["users"].items()}
        self.by_handle = {u["handle"]: uid for uid, u in state["users"].items()}

    def _next_seq(self) -> int:
        self.state["seq"] += 1
        return self.state["seq"]

    def _new_id(self, prefix: str, table: str) -> str:
        counters = self.state["counters"]
        n = counters.get(prefix, 0)
        existing = self.state[table]
        while True:
            n += 1
            candidate = f"{prefix}_{n}"
            if candidate not in existing:
                counters[prefix] = n
                return candidate

    def _user_by_handle(self, handle: str) -> dict:
        uid = self.by_handle.get(handle)
        if uid is None:
            raise ApiError(404, "not_found", "no user has that handle")
        return self.state["users"][uid]

    # ---- reset / export / import ----
    def reset(self, fixture) -> None:
        state = build_from_fixture(fixture)
        with self.lock:
            self._install(state)

    def export(self) -> dict:
        with self.lock:
            snapshot = copy.deepcopy(self.state)
        return {"track": TRACK, "format_version": FORMAT_VERSION, "state": snapshot}

    def import_(self, document) -> None:
        if not isinstance(document, dict):
            raise validation("import body must be an object")
        if document.get("track") != TRACK:
            raise validation("wrong track")
        version = document.get("format_version")
        if not is_number(version) or integral(version) != FORMAT_VERSION:
            raise validation("unsupported format_version")
        state = validate_state(document.get("state"))
        with self.lock:
            self._install(state)

    # ---- auth ----
    def signup(self, body: dict) -> dict:
        email, password, display_name = body.get("email"), body.get("password"), body.get("display_name")
        for name, value in (("email", email), ("password", password), ("display_name", display_name)):
            if name not in body:
                raise validation(f"{name} is required")
            if not isinstance(value, str):
                raise ApiError(400, "malformed_request", f"{name} must be a string")
        if not EMAIL_RE.match(email):
            raise validation("email must look like local@domain")
        if len(password) < 8:
            raise validation("password must be at least 8 characters")
        handle = derive_handle(email)
        if not HANDLE_RE.match(handle):
            raise validation("cannot derive a handle from that email")
        self._check_signup_free(email, handle)
        hashed = hash_password(password)
        with self.lock:
            self._check_signup_free(email, handle)
            uid = self._new_id("u", "users")
            self.state["users"][uid] = {
                "id": uid, "email": email, "password_hash": hashed,
                "display_name": display_name, "handle": handle, "balance": 0,
            }
            self.by_email[email.lower()] = uid
            self.by_handle[handle] = uid
            token = self._issue_token(uid)
        return {"user_id": uid, "display_name": display_name, "token": token}

    def _check_signup_free(self, email: str, handle: str) -> None:
        with self.lock:
            if email.lower() in self.by_email:
                raise ApiError(409, "email_taken", "email already registered")
            if handle in self.by_handle:
                raise ApiError(409, "handle_taken", "derived handle already taken")

    def login(self, body: dict) -> dict:
        email, password = body.get("email"), body.get("password")
        for name, value in (("email", email), ("password", password)):
            if name not in body:
                raise validation(f"{name} is required")
            if not isinstance(value, str):
                raise ApiError(400, "malformed_request", f"{name} must be a string")
        with self.lock:
            uid = self.by_email.get(email.lower())
            hashed = self.state["users"][uid]["password_hash"] if uid else None
        if hashed is None or not verify_password(password, hashed):
            raise ApiError(401, "unauthenticated", "wrong email or password")
        with self.lock:
            user = self.state["users"].get(uid)
            if user is None or user["password_hash"] != hashed:
                raise ApiError(401, "unauthenticated", "wrong email or password")
            token = self._issue_token(uid)
            return {"user_id": uid, "display_name": user["display_name"], "token": token}

    def _issue_token(self, uid: str) -> str:
        token = secrets.token_urlsafe(32)
        self.state["tokens"][token] = uid
        return token

    def authenticate(self, token: str | None) -> dict:
        with self.lock:
            uid = self.state["tokens"].get(token) if token else None
            user = self.state["users"].get(uid) if uid else None
            if user is None:
                raise ApiError(401, "unauthenticated", "missing or unknown bearer token")
            return user

    def is_operator(self, uid: str) -> bool:
        with self.lock:
            return uid in self.state["operators"]

    # ---- idempotency ----
    def idempotent(self, uid: str, method: str, path: str, key: str, body: dict, action):
        """Resolve a claimed key or run `action` exactly once. Returns (status, body)."""
        slot = "\x1f".join((uid, method, path, key))
        text = canonical(body)
        with self.lock:
            record = self.state["idempotency"].get(slot)
            if record is not None:
                if record["body"] != text:
                    raise ApiError(409, "idempotency_key_reuse",
                                   "key already used with a different request body")
                return 200, copy.deepcopy(record["response"])
            response = action()   # raises ApiError on failure: key stays unclaimed
            self.state["idempotency"][slot] = {
                "body": text, "status": 201, "response": copy.deepcopy(response)}
            return 201, response

    # ---- reads ----
    def me(self, user: dict) -> dict:
        with self.lock:
            u = self.state["users"][user["id"]]
            return {"user_id": u["id"], "display_name": u["display_name"], "handle": u["handle"],
                    "balance": u["balance"], "currency": self.state["currency"],
                    "minor_units": self.state["minor_units"]}

    def activity(self, user: dict, limit: int, offset: int) -> dict:
        uid = user["id"]
        with self.lock:
            visible = [p for p in self.state["payments"].values()
                       if p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])]
            visible.sort(key=lambda p: (p["_ts"], p["_seq"]), reverse=True)
            page = visible[offset:offset + limit]
            return {"payments": [public(p) for p in page],
                    "has_more": len(visible) > offset + limit}

    def list_requests(self, user: dict, direction, status, limit: int, offset: int) -> dict:
        uid = user["id"]
        with self.lock:
            rows = []
            for r in self.state["requests"].values():
                if direction == "incoming" and r["payer_id"] != uid:
                    continue
                if direction == "outgoing" and r["requester_id"] != uid:
                    continue
                if direction is None and uid not in (r["payer_id"], r["requester_id"]):
                    continue
                if status is not None and r["status"] != status:
                    continue
                rows.append(r)
            rows.sort(key=lambda r: (r["_ts"], r["_seq"]), reverse=True)
            page = rows[offset:offset + limit]
            return {"requests": [public(r) for r in page],
                    "has_more": len(rows) > offset + limit}

    # ---- writes (callers hold the lock via `idempotent`, or take it here) ----
    def _make_payment(self, sender: dict, receiver: dict, amount: int, note: str,
                      visibility: str, *, request_id=None, settlement_id=None,
                      created_at=None) -> dict:
        created_at = created_at or now_iso()
        pid = self._new_id("p", "payments")
        record = {
            "payment_id": pid,
            "from_user_id": sender["id"], "from_handle": sender["handle"],
            "to_user_id": receiver["id"], "to_handle": receiver["handle"],
            "amount": amount, "currency": self.state["currency"],
            "note": note, "visibility": visibility,
            "request_id": request_id, "settlement_id": settlement_id,
            "created_at": created_at,
            "_ts": parse_ts(created_at), "_seq": self._next_seq(),
        }
        self.state["payments"][pid] = record
        return record

    def _transfer(self, sender: dict, receiver: dict, amount: int) -> None:
        if sender["balance"] < amount:
            raise ApiError(409, "insufficient_funds", "balance is below amount")
        sender["balance"] -= amount
        receiver["balance"] += amount

    def create_payment(self, user: dict, body: dict) -> dict:
        to_handle = handle_field(body, "to_handle")
        amount = amount_field(body)
        note = note_field(body)
        visibility = visibility_field(body)
        with self.lock:
            sender = self.state["users"][user["id"]]
            if to_handle == sender["handle"]:
                raise ApiError(422, "self_payment", "cannot pay yourself")
            receiver = self._user_by_handle(to_handle)
            self._transfer(sender, receiver, amount)
            return public(self._make_payment(sender, receiver, amount, note, visibility))

    def _make_request(self, requester: dict, payer: dict, amount: int, note: str,
                      created_at=None) -> dict:
        created_at = created_at or now_iso()
        rid = self._new_id("rq", "requests")
        record = {
            "request_id": rid,
            "requester_id": requester["id"], "requester_handle": requester["handle"],
            "payer_id": payer["id"], "payer_handle": payer["handle"],
            "amount": amount, "currency": self.state["currency"], "note": note,
            "status": "pending", "payment_id": None, "created_at": created_at,
            "_ts": parse_ts(created_at), "_seq": self._next_seq(),
        }
        self.state["requests"][rid] = record
        return record

    def create_request(self, user: dict, body: dict) -> dict:
        payer_handle = handle_field(body, "payer_handle")
        amount = amount_field(body)
        note = note_field(body)
        with self.lock:
            requester = self.state["users"][user["id"]]
            if payer_handle == requester["handle"]:
                raise ApiError(422, "self_request", "cannot request from yourself")
            payer = self._user_by_handle(payer_handle)
            return public(self._make_request(requester, payer, amount, note))

    def _request_for(self, user: dict, request_id: str) -> dict:
        r = self.state["requests"].get(request_id)
        if r is None or user["id"] not in (r["payer_id"], r["requester_id"]):
            raise ApiError(404, "not_found", "no such request")
        return r

    def pay_request(self, user: dict, request_id: str, body: dict) -> dict:
        visibility = visibility_field(body)
        with self.lock:
            r = self._request_for(user, request_id)
            if r["payer_id"] != user["id"]:
                raise ApiError(403, "forbidden", "only the payer may pay a request")
            if r["status"] != "pending":
                raise ApiError(409, "request_not_pending", "request is not pending")
            payer = self.state["users"][r["payer_id"]]
            requester = self.state["users"][r["requester_id"]]
            self._transfer(payer, requester, r["amount"])
            payment = self._make_payment(payer, requester, r["amount"], r["note"], visibility,
                                         request_id=r["request_id"])
            r["status"] = "paid"
            r["payment_id"] = payment["payment_id"]
            return public(payment)

    def resolve_request(self, user: dict, request_id: str, target: str) -> dict:
        """Decline (payer only) or cancel (requester only). Repeating is a no-op."""
        with self.lock:
            r = self._request_for(user, request_id)
            owner = r["payer_id"] if target == "declined" else r["requester_id"]
            if owner != user["id"]:
                raise ApiError(403, "forbidden", "not permitted on this request")
            if r["status"] == target:
                return public(r)
            if r["status"] != "pending":
                raise ApiError(409, "request_not_pending", "request is not pending")
            r["status"] = target
            return public(r)

    def create_split(self, user: dict, body: dict) -> dict:
        amount = amount_field(body)
        if "participant_handles" not in body:
            raise validation("participant_handles is required")
        handles = body["participant_handles"]
        if not isinstance(handles, list) or not all(isinstance(h, str) for h in handles):
            raise ApiError(400, "malformed_request", "participant_handles must be a list of strings")
        if not handles:
            raise validation("participant_handles must not be empty")
        if len(set(handles)) != len(handles):
            raise validation("participant_handles contains a duplicate")
        for h in handles:
            if not HANDLE_RE.match(h):
                raise validation("participant_handles contains an invalid handle")
        note = note_field(body)
        with self.lock:
            requester = self.state["users"][user["id"]]
            participants = [self._user_by_handle(h) for h in handles]
            shares = equal_split(amount, len(handles))
            created_at = now_iso()
            requests = [public(self._make_request(requester, p, share, note, created_at))
                        for p, share in zip(participants, shares) if p["id"] != requester["id"]]
            sid = self._new_id("sp", "splits")
            response = {
                "split_id": sid, "amount": amount, "currency": self.state["currency"],
                "note": note,
                "shares": [{"handle": h, "amount": s} for h, s in zip(handles, shares)],
                "requests": requests, "created_at": created_at,
            }
            self.state["splits"][sid] = {"split_id": sid, "requester_id": requester["id"],
                                         "request_ids": [r["request_id"] for r in requests]}
            return response

    def create_settlement(self, user: dict, body: dict) -> dict:
        transfers = body.get("transfers")
        if not isinstance(transfers, list) or not 1 <= len(transfers) <= MAX_TRANSFERS:
            raise validation(f"transfers must be a list of 1 to {MAX_TRANSFERS} objects")
        with self.lock:
            entries = []
            for entry in transfers:   # first bad entry, in input order, decides the error
                if not isinstance(entry, dict):
                    raise validation("each transfer must be an object")
                from_handle = handle_field(entry, "from_handle", wrong_type_status=422)
                to_handle = handle_field(entry, "to_handle", wrong_type_status=422)
                amount = amount_field(entry)
                note = note_field(entry)
                visibility = visibility_field(entry)
                if from_handle == to_handle:
                    raise ApiError(422, "self_payment", "a transfer cannot pay its own sender")
                sender = self._user_by_handle(from_handle)
                receiver = self._user_by_handle(to_handle)
                entries.append((sender, receiver, amount, note, visibility))
            net: dict[str, int] = {}
            for sender, receiver, amount, _, _ in entries:
                net[sender["id"]] = net.get(sender["id"], 0) - amount
                net[receiver["id"]] = net.get(receiver["id"], 0) + amount
            users = self.state["users"]
            if any(users[uid]["balance"] + delta < 0 for uid, delta in net.items()):
                raise ApiError(409, "insufficient_funds", "settlement is not affordable")
            for uid, delta in net.items():
                users[uid]["balance"] += delta
            committed_at = now_iso()
            sid = self._new_id("st", "settlements")
            payments = [public(self._make_payment(s, r, a, n, v, settlement_id=sid,
                                                  created_at=committed_at))
                        for s, r, a, n, v in entries]
            self.state["settlements"][sid] = {
                "settlement_id": sid, "committed_at": committed_at,
                "operator_id": user["id"],
                "payment_ids": [p["payment_id"] for p in payments]}
            return {"settlement_id": sid, "committed_at": committed_at, "payments": payments}


def equal_split(amount: int, n: int) -> list[int]:
    base, remainder = divmod(amount, n)
    return [base + (1 if i < remainder else 0) for i in range(n)]


# ------------------------------------------------- fixture & import loading

def _req_str(obj: dict, key: str, *, max_len: int | None = None) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or (max_len is not None and not 1 <= len(value) <= max_len):
        raise validation(f"{key} must be a string")
    return value


def _req_int(obj: dict, key: str) -> int:
    value = obj.get(key)
    as_int = integral(value) if is_number(value) else None
    if as_int is None:
        raise validation(f"{key} must be an integer")
    return as_int


def build_from_fixture(fixture) -> dict:
    """Validate a reset fixture and build a complete new state. Raises ApiError
    without side effects on any problem."""
    if not isinstance(fixture, dict):
        raise validation("fixture must be an object")
    state = empty_state()
    currency = fixture.get("currency", "EUR")
    if not isinstance(currency, str) or not currency:
        raise validation("currency must be a string")
    minor_units = _req_int(fixture, "minor_units") if "minor_units" in fixture else 2
    if minor_units not in MINOR_UNITS:
        raise validation("minor_units must be 0, 2 or 3")
    state["currency"], state["minor_units"] = currency, minor_units

    users_in = fixture.get("users", [])
    if not isinstance(users_in, list):
        raise validation("users must be a list")
    hash_cache: dict[str, str] = {}
    by_email, by_handle = set(), set()
    for u in users_in:
        if not isinstance(u, dict):
            raise validation("each user must be an object")
        uid = _req_str(u, "id", max_len=MAX_ID)
        email = _req_str(u, "email")
        password = _req_str(u, "password")
        handle = _req_str(u, "handle")
        display_name = u.get("display_name", "")
        if not isinstance(display_name, str):
            raise validation("display_name must be a string")
        if not HANDLE_RE.match(handle):
            raise validation("invalid handle")
        balance = _req_int(u, "balance")
        if balance < 0:
            raise validation("seeded balance cannot be negative")
        if uid in state["users"] or email.lower() in by_email or handle in by_handle:
            raise validation("duplicate user id, email or handle")
        by_email.add(email.lower())
        by_handle.add(handle)
        if password not in hash_cache:
            hash_cache[password] = hash_password(password)
        state["users"][uid] = {"id": uid, "email": email, "password_hash": hash_cache[password],
                               "display_name": display_name, "handle": handle,
                               "balance": balance}

    users = state["users"]
    reset_at = now_iso()

    payments_in = fixture.get("payments") or []
    if not isinstance(payments_in, list):
        raise validation("payments must be a list")
    for p in payments_in:
        if not isinstance(p, dict):
            raise validation("each payment must be an object")
        pid = _req_str(p, "id", max_len=MAX_ID)
        sender, receiver = users.get(p.get("from_user_id")), users.get(p.get("to_user_id"))
        if sender is None or receiver is None or pid in state["payments"]:
            raise validation("payment references unknown users or repeats an id")
        amount = _req_int(p, "amount")
        note = note_field(p) if p.get("note") is not None else ""
        visibility = visibility_field(p) if p.get("visibility") is not None else "public"
        created_at = p.get("created_at") or reset_at
        try:
            ts = parse_ts(created_at)
        except ValueError:
            raise validation("invalid payment created_at") from None
        state["seq"] += 1
        state["payments"][pid] = {
            "payment_id": pid,
            "from_user_id": sender["id"], "from_handle": sender["handle"],
            "to_user_id": receiver["id"], "to_handle": receiver["handle"],
            "amount": amount, "currency": currency, "note": note, "visibility": visibility,
            "request_id": p.get("request_id"), "settlement_id": p.get("settlement_id"),
            "created_at": created_at, "_ts": ts, "_seq": state["seq"],
        }

    requests_in = fixture.get("requests") or []
    if not isinstance(requests_in, list):
        raise validation("requests must be a list")
    for r in requests_in:
        if not isinstance(r, dict):
            raise validation("each request must be an object")
        rid = _req_str(r, "id", max_len=MAX_ID)
        requester, payer = users.get(r.get("requester_id")), users.get(r.get("payer_id"))
        if requester is None or payer is None or rid in state["requests"]:
            raise validation("request references unknown users or repeats an id")
        amount = _req_int(r, "amount")
        status = r.get("status", "pending")
        if status not in REQUEST_STATUSES:
            raise validation("invalid request status")
        note = note_field(r) if r.get("note") is not None else ""
        created_at = r.get("created_at") or reset_at
        try:
            ts = parse_ts(created_at)
        except ValueError:
            raise validation("invalid request created_at") from None
        state["seq"] += 1
        state["requests"][rid] = {
            "request_id": rid,
            "requester_id": requester["id"], "requester_handle": requester["handle"],
            "payer_id": payer["id"], "payer_handle": payer["handle"],
            "amount": amount, "currency": currency, "note": note, "status": status,
            "payment_id": r.get("payment_id"), "created_at": created_at,
            "_ts": ts, "_seq": state["seq"],
        }

    operators = fixture.get("settlement_operator_ids") or []
    if not isinstance(operators, list) or not all(isinstance(o, str) for o in operators):
        raise validation("settlement_operator_ids must be a list of user ids")
    state["operators"] = list(dict.fromkeys(operators))
    return state


def validate_state(state) -> dict:
    """Check an exported state thoroughly before it may replace live state."""
    try:
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        expected = empty_state()
        for key, default in expected.items():
            if key not in state or type(state[key]) is not type(default):
                raise ValueError(f"state.{key} missing or wrong type")
        if state["minor_units"] not in MINOR_UNITS or isinstance(state["minor_units"], bool):
            raise ValueError("minor_units")
        if not isinstance(state["currency"], str):
            raise ValueError("currency")
        emails, handles = set(), set()
        for uid, u in state["users"].items():
            if not isinstance(u, dict) or u.get("id") != uid:
                raise ValueError("user id")
            for f in ("email", "display_name", "handle", "password_hash"):
                if not isinstance(u.get(f), str):
                    raise ValueError(f"user {f}")
            if not looks_hashed(u["password_hash"]) or not HANDLE_RE.match(u["handle"]):
                raise ValueError("user credentials or handle")
            if type(u.get("balance")) is not int or u["balance"] < 0:
                raise ValueError("user balance")
            if u["email"].lower() in emails or u["handle"] in handles:
                raise ValueError("duplicate user")
            emails.add(u["email"].lower())
            handles.add(u["handle"])
        users = state["users"]
        for token, uid in state["tokens"].items():
            if uid not in users:
                raise ValueError("token user")
        for table, id_key, parties in (("payments", "payment_id", ("from_user_id", "to_user_id")),
                                       ("requests", "request_id", ("requester_id", "payer_id"))):
            for rid, rec in state[table].items():
                if not isinstance(rec, dict) or rec.get(id_key) != rid:
                    raise ValueError(f"{table} id")
                if any(rec.get(p) not in users for p in parties):
                    raise ValueError(f"{table} party")
                if type(rec.get("amount")) is not int or type(rec.get("_seq")) is not int:
                    raise ValueError(f"{table} amount")
                if type(rec.get("_ts")) is not int:
                    raise ValueError(f"{table} timestamp")
                parse_ts(rec.get("created_at"))
        for r in state["requests"].values():
            if r.get("status") not in REQUEST_STATUSES:
                raise ValueError("request status")
        for p in state["payments"].values():
            if p.get("visibility") not in VISIBILITIES:
                raise ValueError("payment visibility")
        for slot, rec in state["idempotency"].items():
            if not isinstance(rec, dict) or not isinstance(rec.get("body"), str) or "response" not in rec:
                raise ValueError("idempotency record")
        if not all(isinstance(o, str) for o in state["operators"]):
            raise ValueError("operators")
        if not all(isinstance(k, str) and type(v) is int for k, v in state["counters"].items()):
            raise ValueError("counters")
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise validation(f"invalid state: {exc}") from None
    return copy.deepcopy(state)
