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
from datetime import datetime, timedelta, timezone
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
AUTH_STATUSES = ("open", "captured", "voided", "expired")
DEFAULT_TTL = 600


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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def now_us() -> int:
    return parse_ts(now_iso())


def plus_seconds(iso: str, seconds: int) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def parse_ts(value) -> int:
    """Epoch microseconds of an RFC 3339 timestamp with an explicit offset."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})", value):
        raise ValueError("timestamp must be an RFC3339 instant with offset")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp needs an offset")
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


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


def iso_us(value):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)).isoformat(timespec="microseconds")


def query_time(query, name, default):
    if name not in query:
        return default
    try:
        return parse_ts(query[name][0])
    except (ValueError, OverflowError):
        raise validation(f"invalid {name}") from None


def original_revision(payment):
    return {"payment_id": payment["payment_id"], "revision": 1, "amount": payment["amount"],
            "effective_at": payment["created_at"], "recorded_at": payment["created_at"], "reason": ""}


def payment_delta(payment, amount, uid):
    return amount * ((payment["to_user_id"] == uid) - (payment["from_user_id"] == uid))


def historical_held(state, uid, at, known):
    held = 0
    for a in state["authorizations"].values():
        if a["from_user_id"] != uid or at >= a["_exp"]:
            continue
        remaining = 0
        for event in a["_events"]:
            time = parse_ts(event["at"])
            if time <= at and time <= known:
                remaining = event["remaining"]
        held += remaining
    return held


def upgrade_history(state):
    """Upgrade our older exports without replaying money or changing receipts.

    Closed legacy holds lack a complete event log; the spec allows seeded closed
    holds without reconstruction. Open holds reconstruct known captures, and keep
    their current reservation even when a seed omits its earlier captures.
    """
    openings = {uid: u["balance"] for uid, u in state["users"].items()}
    for p in state["payments"].values():
        p.setdefault("_revisions", [original_revision(p)])
        amount = p["_revisions"][-1]["amount"]
        openings[p["from_user_id"]] += amount
        openings[p["to_user_id"]] -= amount
    for uid, u in state["users"].items():
        u.setdefault("_opening", openings[uid])
    for a in state["authorizations"].values():
        if "_events" not in a:
            events = []
            if a["status"] == "open":
                captures = sorted((p for p in state["payments"].values()
                                   if p.get("authorization_id") == a["authorization_id"]),
                                  key=lambda p: (parse_ts(p["created_at"]), p["_seq"]))
                remaining = a["remaining_amount"] + sum(p["amount"] for p in captures)
                events.append({"at": a["created_at"], "remaining": remaining})
                for p in captures:
                    remaining -= p["amount"]
                    events.append({"at": p["created_at"], "remaining": remaining})
            a["_events"] = events
        a.setdefault("closed_at", None if a["status"] == "open" else
                     a["expires_at"] if a["status"] == "expired" else a["created_at"])


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
        "authorizations": {},
        "authorization_ttl_seconds": DEFAULT_TTL,
    }


# Keys added after the first release; an older export may omit them.
UPGRADE_DEFAULTS = {"authorizations": {}, "authorization_ttl_seconds": DEFAULT_TTL}


# ------------------------------------------------------------------ store

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._install(empty_state())

    # ---- internal state management ----
    def _install(self, state: dict) -> None:
        """Swap in a fully-built state and rebuild indexes. Caller holds the lock
        (or is the constructor)."""
        upgrade_history(state)
        self.state = state
        self.snapshots = {}
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
            statement_snapshots = copy.deepcopy(self.snapshots)
        return {"track": TRACK, "format_version": FORMAT_VERSION, "state": snapshot,
                "snapshots": statement_snapshots}

    def import_(self, document) -> None:
        if not isinstance(document, dict):
            raise validation("import body must be an object")
        if document.get("track") != TRACK:
            raise validation("wrong track")
        version = document.get("format_version")
        if not is_number(version) or integral(version) != FORMAT_VERSION:
            raise validation("unsupported format_version")
        state = validate_state(document.get("state"))
        raw_snapshots = document.get("snapshots", {})
        if not isinstance(raw_snapshots, dict):
            raise validation("snapshots must be an object")
        snapshots = {}
        for token, snap in raw_snapshots.items():
            if (not isinstance(token, str) or not isinstance(snap, dict)
                    or snap.get("uid") not in state["users"] or not isinstance(snap.get("result"), dict)
                    or not isinstance(snap["result"].get("entries"), list)):
                raise validation("invalid statement snapshot")
            snapshots[token] = copy.deepcopy(snap)
        with self.lock:
            self._install(state)
            self.snapshots = snapshots

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
                "display_name": display_name, "handle": handle, "balance": 0, "_opening": 0,
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

    def _live(self, user: dict) -> dict:
        """Re-resolve an authenticated caller in the current state (caller holds
        the lock). A reset/import between authentication and use can remove the
        account; that is a stale credential (401), never a server error."""
        live = self.state["users"].get(user["id"])
        if live is None:
            raise ApiError(401, "unauthenticated", "account no longer exists")
        return live

    # ---- holds (caller holds the lock) ----
    def _sweep(self) -> None:
        """Apply clock expiry: every open authorization at or past its deadline
        becomes expired and stops holding funds. Run before any read or write
        that depends on holds, so expiry is visible without a request at the
        deadline."""
        now = now_us()
        for a in self.state["authorizations"].values():
            if a["status"] == "open" and a["_exp"] <= now:
                a["status"] = "expired"
                a["remaining_amount"] = 0
                a["closed_at"] = a["expires_at"]

    def _held(self, uid: str) -> int:
        return sum(a["remaining_amount"] for a in self.state["authorizations"].values()
                   if a["status"] == "open" and a["from_user_id"] == uid)

    def _available(self, user: dict) -> int:
        return user["balance"] - self._held(user["id"])

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
    def me(self, user: dict, query=None) -> dict:
        query = query or {}
        with self.lock:
            instant = now_us()
            u = self._live(user)
            self._sweep()
            at = query_time(query, "as_of", instant)
            known = query_time(query, "known_at", instant)
            balance = u["_opening"]
            for p, r in self._selected(known):
                if parse_ts(r["effective_at"]) <= at:
                    balance += payment_delta(p, r["amount"], u["id"])
            held = historical_held(self.state, u["id"], at, known)
            result = {"user_id": u["id"], "display_name": u["display_name"], "handle": u["handle"],
                    "balance": balance, "total": balance,
                    "available": balance - held, "held": held,
                    "currency": self.state["currency"],
                    "minor_units": self.state["minor_units"]}
            for field in ("as_of", "known_at"):
                if field in query:
                    result[field] = query[field][0]
            return result

    def _selected(self, known, replacement=None):
        rows = []
        for p in self.state["payments"].values():
            history = p["_revisions"]
            if replacement and p["payment_id"] in replacement:
                history = history + [replacement[p["payment_id"]]]
            r = next((r for r in reversed(history) if parse_ts(r["recorded_at"]) <= known), None)
            if r is not None:
                rows.append((p, r))
        return rows

    def statement(self, user, query, limit, offset):
        with self.lock:
            instant = now_us()
            uid = self._live(user)["id"]
            if "snapshot" in query:
                if any(k in query for k in ("from", "to", "known_at")):
                    raise validation("snapshot cannot be combined with temporal filters")
                token = query["snapshot"][0]
                snapshot = self.snapshots.get(token)
                if snapshot is None or snapshot["uid"] != uid:
                    raise ApiError(404, "not_found")
                result = copy.deepcopy(snapshot["result"])
            else:
                start = query_time(query, "from", None)
                end = query_time(query, "to", instant)
                known = query_time(query, "known_at", instant)
                if start is not None and start > end:
                    raise validation("from cannot follow to")
                rows = [(p, r) for p, r in self._selected(known)
                        if uid in (p["from_user_id"], p["to_user_id"])]
                rows.sort(key=lambda row: (parse_ts(row[1]["effective_at"]), row[0]["payment_id"]))
                balance = self.state["users"][uid]["_opening"]
                opening = balance
                entries = []
                for p, r in rows:
                    at = parse_ts(r["effective_at"])
                    if at >= end:
                        break
                    delta = payment_delta(p, r["amount"], uid)
                    balance += delta
                    if start is not None and at < start:
                        opening = balance
                        continue
                    payment = copy.deepcopy(public(p))
                    payment["amount"] = r["amount"]
                    entries.append({"payment": payment, "delta": delta, "balance_after": balance,
                                    **{k: r[k] for k in ("revision", "effective_at", "recorded_at")}})
                token = secrets.token_urlsafe(32)
                result = {"opening_balance": opening, "closing_balance": balance,
                          "entries": entries, "snapshot": token}
                if "known_at" in query:
                    result["known_at"] = query["known_at"][0]
                self.snapshots[token] = {"uid": uid, "result": copy.deepcopy(result)}
            entries = result["entries"]
            result["has_more"] = offset + limit < len(entries)
            result["entries"] = entries[offset:offset + limit]
            return result

    def revisions(self, user, pid):
        with self.lock:
            uid = self._live(user)["id"]
            p = self.state["payments"].get(pid)
            if p is None or uid not in (p["from_user_id"], p["to_user_id"]):
                raise ApiError(404, "not_found")
            return {"revisions": copy.deepcopy(p["_revisions"])}

    def correct_payment(self, user, pid, body):
        with self.lock:
            uid = self._live(user)["id"]
            p = self.state["payments"].get(pid)
            if p is None:
                raise ApiError(404, "not_found")
            if p["from_user_id"] != uid:
                raise ApiError(403, "forbidden")
            if p.get("settlement_id") or p.get("authorization_id"):
                raise ApiError(422, "linked_payment_immutable")
            expected = integral(body.get("expected_revision"))
            amount = integral(body.get("amount"))
            reason = body.get("reason")
            effective = body.get("effective_at")
            try:
                at = parse_ts(effective)
            except (ValueError, OverflowError):
                raise validation("invalid effective_at") from None
            instant = now_us()
            if (expected is None or expected < 1 or amount is None or not 0 <= amount <= MAX_AMOUNT
                    or not isinstance(reason, str) or not 1 <= len(reason) <= 200 or at > instant):
                raise validation("invalid correction")
            previous = p["_revisions"][-1]
            if expected != previous["revision"]:
                raise ApiError(409, "stale_revision")
            difference = amount - previous["amount"]
            debit_id = p["from_user_id"] if difference >= 0 else p["to_user_id"]
            # No lifecycle mutation on failure; expiry is evaluated as of this instant.
            debit = self.state["users"][debit_id]
            available = debit["balance"] - historical_held(self.state, debit_id, instant, instant)
            if available < abs(difference):
                raise ApiError(409, "insufficient_funds")
            recorded = max(instant, parse_ts(previous["recorded_at"]) + 1)
            revision = {"payment_id": pid, "revision": expected + 1, "amount": amount,
                        "effective_at": effective, "recorded_at": iso_us(recorded), "reason": reason}
            self._check_history({pid: revision}, recorded)
            self.state["users"][p["from_user_id"]]["balance"] -= difference
            self.state["users"][p["to_user_id"]]["balance"] += difference
            p["_revisions"].append(revision)
            return copy.deepcopy(revision)

    def _check_history(self, replacements, instant):
        # Group every simultaneous movement and hold event before checking solvency.
        balances = {uid: u["_opening"] for uid, u in self.state["users"].items()}
        events = {}
        def add(at, uid, total=0, held=0):
            row = events.setdefault(at, {}).setdefault(uid, [0, 0])
            row[0] += total
            row[1] += held
        for p, r in self._selected(instant, replacements):
            at = parse_ts(r["effective_at"])
            add(at, p["from_user_id"], -r["amount"])
            add(at, p["to_user_id"], r["amount"])
        for a in self.state["authorizations"].values():
            previous = 0
            for event in a["_events"]:
                at = parse_ts(event["at"])
                if at > instant or at >= a["_exp"]:
                    continue
                add(at, a["from_user_id"], held=event["remaining"] - previous)
                previous = event["remaining"]
            if a["_exp"] <= instant:
                add(a["_exp"], a["from_user_id"], held=-previous)
        held = {uid: 0 for uid in balances}
        if any(v < 0 for v in balances.values()):
            raise ApiError(409, "historical_overdraft")
        for at, changes in sorted(events.items()):
            if at > instant:
                break
            for uid, (delta, reservation) in changes.items():
                balances[uid] += delta
                held[uid] += reservation
            if any(balances[uid] < 0 or balances[uid] - held[uid] < 0 for uid in changes):
                raise ApiError(409, "historical_overdraft")

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
                      authorization_id=None, created_at=None) -> dict:
        created_at = created_at or now_iso()
        pid = self._new_id("p", "payments")
        record = {
            "payment_id": pid,
            "from_user_id": sender["id"], "from_handle": sender["handle"],
            "to_user_id": receiver["id"], "to_handle": receiver["handle"],
            "amount": amount, "currency": self.state["currency"],
            "note": note, "visibility": visibility,
            "request_id": request_id, "settlement_id": settlement_id,
            "authorization_id": authorization_id,
            "created_at": created_at,
            "_ts": parse_ts(created_at), "_seq": self._next_seq(),
        }
        self.state["payments"][pid] = record
        record["_revisions"] = [original_revision(record)]
        return record

    def _transfer(self, sender: dict, receiver: dict, amount: int) -> None:
        """Immediate transfer funded from the sender's available (unheld) money."""
        self._sweep()
        if self._available(sender) < amount:
            raise ApiError(409, "insufficient_funds", "available balance is below amount")
        sender["balance"] -= amount
        receiver["balance"] += amount

    def create_payment(self, user: dict, body: dict) -> dict:
        to_handle = handle_field(body, "to_handle")
        amount = amount_field(body)
        note = note_field(body)
        visibility = visibility_field(body)
        with self.lock:
            sender = self._live(user)
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
            requester = self._live(user)
            if payer_handle == requester["handle"]:
                raise ApiError(422, "self_request", "cannot request from yourself")
            payer = self._user_by_handle(payer_handle)
            return public(self._make_request(requester, payer, amount, note))

    def _request_for(self, user: dict, request_id: str) -> dict:
        # Existence is checked here; per-action ownership (403 for anyone else,
        # third parties included) is checked by the caller.
        r = self.state["requests"].get(request_id)
        if r is None:
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
            requester = self._live(user)
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
            self._sweep()
            # Held funds cannot fund a net debit: compare against available.
            if any(self._available(users[uid]) + delta < 0 for uid, delta in net.items()):
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

    # ---- authorizations ----
    def create_authorization(self, user: dict, body: dict) -> dict:
        to_handle = handle_field(body, "to_handle")
        amount = amount_field(body)
        note = note_field(body)
        visibility = visibility_field(body)
        with self.lock:
            payer = self._live(user)
            if to_handle == payer["handle"]:
                raise ApiError(422, "self_payment", "cannot authorize a payment to yourself")
            receiver = self._user_by_handle(to_handle)
            self._sweep()
            if self._available(payer) < amount:
                raise ApiError(409, "insufficient_funds", "available balance is below amount")
            created_at = now_iso()
            expires_at = plus_seconds(created_at, self.state["authorization_ttl_seconds"])
            aid = self._new_id("a", "authorizations")
            record = {
                "authorization_id": aid,
                "from_user_id": payer["id"], "from_handle": payer["handle"],
                "to_user_id": receiver["id"], "to_handle": receiver["handle"],
                "amount": amount, "captured_amount": 0, "remaining_amount": amount,
                "currency": self.state["currency"], "note": note, "visibility": visibility,
                "status": "open", "expires_at": expires_at,
                "payment_id": None, "payment_ids": [], "created_at": created_at,
                "_ts": parse_ts(created_at), "_seq": self._next_seq(),
                "_exp": parse_ts(expires_at),
                "closed_at": None,
                "_events": [{"at": created_at, "remaining": amount}],
            }
            self.state["authorizations"][aid] = record
            return public(record)

    def _authorization(self, authorization_id: str) -> dict:
        a = self.state["authorizations"].get(authorization_id)
        if a is None:
            raise ApiError(404, "not_found", "no such authorization")
        return a

    def capture_authorization(self, user: dict, authorization_id: str, body: dict) -> dict:
        amount = None
        if "amount" in body:
            value = body["amount"]
            amount = integral(value) if is_number(value) else None
            if amount is None or amount < 1:
                raise validation("amount must be a positive integer number of minor units")
        final = body.get("final", True)
        if not isinstance(final, bool):
            raise ApiError(400, "malformed_request", "final must be a boolean")
        with self.lock:
            self._sweep()
            a = self._authorization(authorization_id)
            if a["to_user_id"] != user["id"]:
                raise ApiError(403, "forbidden", "only the receiver may capture")
            if a["status"] == "expired":
                raise ApiError(409, "authorization_expired", "authorization has expired")
            if a["status"] != "open":
                raise ApiError(409, "authorization_not_open", "authorization is not open")
            remaining = a["remaining_amount"]
            if amount is None:
                amount = remaining
            if amount > remaining:
                raise ApiError(422, "capture_exceeds_authorization",
                               "amount exceeds the uncaptured remainder")
            payer = self.state["users"][a["from_user_id"]]
            receiver = self.state["users"][a["to_user_id"]]
            # The money was reserved for this capture, so it is spent from the hold.
            payer["balance"] -= amount
            receiver["balance"] += amount
            payment = self._make_payment(payer, receiver, amount, a["note"], a["visibility"],
                                         authorization_id=a["authorization_id"])
            a["captured_amount"] += amount
            a["payment_id"] = payment["payment_id"]
            a["payment_ids"].append(payment["payment_id"])
            if final or a["captured_amount"] == a["amount"]:
                a["status"] = "captured"
                a["remaining_amount"] = 0
            else:
                a["remaining_amount"] = a["amount"] - a["captured_amount"]
            a["_events"].append({"at": payment["created_at"], "remaining": a["remaining_amount"]})
            if a["status"] != "open":
                a["closed_at"] = payment["created_at"]
            return public(payment)

    def void_authorization(self, user: dict, authorization_id: str) -> dict:
        with self.lock:
            self._sweep()
            a = self._authorization(authorization_id)
            if a["from_user_id"] != user["id"]:
                raise ApiError(403, "forbidden", "only the payer may void")
            if a["status"] == "voided":
                return public(a)
            if a["status"] != "open":
                raise ApiError(409, "authorization_not_open", "authorization is not open")
            a["status"] = "voided"
            a["remaining_amount"] = 0
            a["closed_at"] = now_iso()
            a["_events"].append({"at": a["closed_at"], "remaining": 0})
            return public(a)

    def list_authorizations(self, user: dict, direction, status, limit: int, offset: int) -> dict:
        uid = user["id"]
        with self.lock:
            self._sweep()
            rows = []
            for a in self.state["authorizations"].values():
                if direction == "outgoing" and a["from_user_id"] != uid:
                    continue
                if direction == "incoming" and a["to_user_id"] != uid:
                    continue
                if direction is None and uid not in (a["from_user_id"], a["to_user_id"]):
                    continue
                if status is not None and a["status"] != status:
                    continue
                rows.append(a)
            rows.sort(key=lambda a: (a["_ts"], a["_seq"]), reverse=True)
            page = rows[offset:offset + limit]
            return {"authorizations": [public(a) for a in page],
                    "has_more": len(rows) > offset + limit}


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
        created_at = p.get("created_at", reset_at)
        try:
            ts = parse_ts(created_at)
            if ts > parse_ts(reset_at):
                raise ValueError("future payment")
        except (ValueError, OverflowError):
            raise validation("invalid payment created_at") from None
        state["seq"] += 1
        state["payments"][pid] = {
            "payment_id": pid,
            "from_user_id": sender["id"], "from_handle": sender["handle"],
            "to_user_id": receiver["id"], "to_handle": receiver["handle"],
            "amount": amount, "currency": currency, "note": note, "visibility": visibility,
            "request_id": p.get("request_id"), "settlement_id": p.get("settlement_id"),
            "authorization_id": p.get("authorization_id"),
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

    if "authorization_ttl_seconds" in fixture:
        ttl = fixture["authorization_ttl_seconds"]
        ttl = integral(ttl) if is_number(ttl) else None
        if ttl is None or ttl < 1:
            raise validation("authorization_ttl_seconds must be a positive integer")
        state["authorization_ttl_seconds"] = ttl

    auths_in = fixture.get("authorizations") or []
    if not isinstance(auths_in, list):
        raise validation("authorizations must be a list")
    now = now_us()
    for a in auths_in:
        if not isinstance(a, dict):
            raise validation("each authorization must be an object")
        aid = _req_str(a, "id", max_len=MAX_ID)
        payer, receiver = users.get(a.get("from_user_id")), users.get(a.get("to_user_id"))
        if payer is None or receiver is None or aid in state["authorizations"]:
            raise validation("authorization references unknown users or repeats an id")
        amount = _req_int(a, "amount")
        if amount < 1:
            raise validation("authorization amount must be positive")
        status = a.get("status", "open")
        if status not in AUTH_STATUSES:
            raise validation("invalid authorization status")
        note = note_field(a) if a.get("note") is not None else ""
        visibility = visibility_field(a) if a.get("visibility") is not None else "public"
        created_at = a.get("created_at") or reset_at
        try:
            ts, exp = parse_ts(created_at), parse_ts(a.get("expires_at"))
        except ValueError:
            raise validation("invalid authorization timestamps") from None
        default_captured = amount if status == "captured" else 0
        captured = a.get("captured_amount", default_captured)
        captured = integral(captured) if is_number(captured) else None
        if captured is None or not 0 <= captured <= amount:
            raise validation("invalid captured_amount")
        if status == "open" and exp <= now:
            status = "expired"
        payment_ids = a.get("payment_ids")
        if not isinstance(payment_ids, list):
            payment_ids = [a["payment_id"]] if a.get("payment_id") else []
        state["seq"] += 1
        state["authorizations"][aid] = {
            "authorization_id": aid,
            "from_user_id": payer["id"], "from_handle": payer["handle"],
            "to_user_id": receiver["id"], "to_handle": receiver["handle"],
            "amount": amount, "captured_amount": captured,
            "remaining_amount": amount - captured if status == "open" else 0,
            "currency": currency, "note": note, "visibility": visibility,
            "status": status, "expires_at": a["expires_at"],
            "payment_id": a.get("payment_id"), "payment_ids": payment_ids,
            "created_at": created_at, "_ts": ts, "_seq": state["seq"], "_exp": exp,
            "_events": ([{"at": created_at, "remaining": amount - captured}]
                        if a.get("status", "open") == "open" else []),
            "closed_at": (None if status == "open" else a["expires_at"] if status == "expired"
                          else a.get("closed_at", created_at)),
        }
    held: dict[str, int] = {}
    for a in state["authorizations"].values():
        if a["status"] == "open":
            held[a["from_user_id"]] = held.get(a["from_user_id"], 0) + a["remaining_amount"]
    if any(users[uid]["balance"] < amount for uid, amount in held.items()):
        raise validation("seeded open holds exceed a balance")
    return state


def validate_state(state) -> dict:
    """Check an exported state thoroughly before it may replace live state."""
    try:
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        # Upgrade an export from an earlier release: add keys it predates.
        state = copy.deepcopy(state)
        for key, default in UPGRADE_DEFAULTS.items():
            state.setdefault(key, copy.deepcopy(default))
        if isinstance(state.get("payments"), dict):
            for p in state["payments"].values():
                if isinstance(p, dict):
                    p.setdefault("authorization_id", None)
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
        ttl = state["authorization_ttl_seconds"]
        if type(ttl) is not int or ttl < 1:
            raise ValueError("authorization_ttl_seconds")
        for aid, a in state["authorizations"].items():
            if not isinstance(a, dict) or a.get("authorization_id") != aid:
                raise ValueError("authorization id")
            if a.get("from_user_id") not in users or a.get("to_user_id") not in users:
                raise ValueError("authorization party")
            if a.get("status") not in AUTH_STATUSES or not isinstance(a.get("payment_ids"), list):
                raise ValueError("authorization status")
            for f in ("amount", "captured_amount", "remaining_amount", "_ts", "_seq", "_exp"):
                if type(a.get(f)) is not int:
                    raise ValueError(f"authorization {f}")
            if a["captured_amount"] + a["remaining_amount"] > a["amount"]:
                raise ValueError("authorization amounts")
            parse_ts(a.get("created_at"))
            parse_ts(a.get("expires_at"))
        held: dict[str, int] = {}
        for a in state["authorizations"].values():
            if a["status"] == "open":
                held[a["from_user_id"]] = held.get(a["from_user_id"], 0) + a["remaining_amount"]
        if any(users[uid]["balance"] < amount for uid, amount in held.items()):
            raise ValueError("holds exceed a balance")
        for slot, rec in state["idempotency"].items():
            if not isinstance(rec, dict) or not isinstance(rec.get("body"), str) or "response" not in rec:
                raise ValueError("idempotency record")
        if not all(isinstance(o, str) for o in state["operators"]):
            raise ValueError("operators")
        if not all(isinstance(k, str) and type(v) is int for k, v in state["counters"].items()):
            raise ValueError("counters")
        upgrade_history(state)
        for uid, u in users.items():
            if type(u["_opening"]) is not int:
                raise ValueError("opening balance")
        computed = {uid: u["_opening"] for uid, u in users.items()}
        for p in state["payments"].values():
            revisions = p["_revisions"]
            if not isinstance(revisions, list) or not revisions or revisions[0] != original_revision(p):
                raise ValueError("original revision")
            previous = None
            for index, r in enumerate(revisions, 1):
                if (r["payment_id"] != p["payment_id"] or type(r["revision"]) is not int
                        or r["revision"] != index or type(r["amount"]) is not int
                        or not 0 <= r["amount"] <= MAX_AMOUNT or not isinstance(r["reason"], str)):
                    raise ValueError("revision")
                recorded = parse_ts(r["recorded_at"])
                parse_ts(r["effective_at"])
                if previous is not None and recorded <= previous:
                    raise ValueError("revision order")
                previous = recorded
            computed[p["from_user_id"]] -= revisions[-1]["amount"]
            computed[p["to_user_id"]] += revisions[-1]["amount"]
        if any(computed[uid] != u["balance"] for uid, u in users.items()):
            raise ValueError("ledger balance")
        for a in state["authorizations"].values():
            if a["closed_at"] is not None:
                parse_ts(a["closed_at"])
            previous = None
            for event in a["_events"]:
                at = parse_ts(event["at"])
                if (type(event["remaining"]) is not int or not 0 <= event["remaining"] <= a["amount"]
                        or (previous is not None and at < previous)):
                    raise ValueError("hold event")
                previous = at
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError) as exc:
        raise validation(f"invalid state: {exc}") from None
    return state
