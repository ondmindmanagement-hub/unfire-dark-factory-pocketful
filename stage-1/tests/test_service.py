"""Builder integration tests, derived from the stage specification.

Starts the service in-process on a free port and talks to it over HTTP using
only the standard library. Run: python3 tests/test_service.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import app  # noqa: E402

SERVER = app.Server(("127.0.0.1", 0), app.Handler)
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"
threading.Thread(target=SERVER.serve_forever, daemon=True).start()

PW = "correct horse"


def user(handle, balance, uid=None):
    return {"id": uid or f"u_{handle}", "email": f"{handle}@example.com", "password": PW,
            "display_name": handle.title(), "handle": handle, "balance": balance}


def fixture(**extra):
    base = {"currency": "EUR", "minor_units": 2,
            "users": [user("ada", 10000), user("bob", 2500), user("cy", 500)],
            "payments": [], "requests": []}
    base.update(extra)
    return base


def call(method, path, body=None, token=None, key=None, raw=None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if key is not None:
        req.add_header("Idempotency-Key", key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode()
            return resp.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as err:
        text = err.read().decode()
        return err.code, (json.loads(text) if text else None)


def new_key():
    return uuid.uuid4().hex


class Base(unittest.TestCase):
    def setUp(self):
        self.reset(fixture())

    def reset(self, fx):
        status, _ = call("POST", "/_test/reset", fx)
        self.assertEqual(status, 204)
        self.fx = fx
        self.tok = {}
        for u in fx["users"]:
            s, body = call("POST", "/auth/login", {"email": u["email"], "password": u["password"]})
            self.assertEqual(s, 200, body)
            self.tok[u["handle"]] = body["token"]

    def bal(self, handle):
        return call("GET", "/me", token=self.tok[handle])[1]["balance"]

    def total(self):
        return sum(self.bal(h) for h in self.tok)

    def assertErr(self, result, status, code):
        s, body = result
        self.assertEqual((s, body["error"]["code"]), (status, code), body)

    def pay(self, frm, to, amount, key=None, **extra):
        return call("POST", "/payments", {"to_handle": to, "amount": amount, **extra},
                    token=self.tok[frm], key=key or new_key())


class Payments(Base):
    def test_payment_moves_money_and_conserves(self):
        s, p = self.pay("ada", "bob", 1500, note="dinner", visibility="private")
        self.assertEqual(s, 201)
        self.assertEqual((p["amount"], p["from_handle"], p["to_handle"], p["visibility"],
                          p["request_id"], p["settlement_id"], p["currency"]),
                         (1500, "ada", "bob", "private", None, None, "EUR"))
        self.assertEqual((self.bal("ada"), self.bal("bob")), (8500, 4000))
        self.assertEqual(self.total(), 13000)

    def test_insufficient_and_exact_balance(self):
        self.assertErr(self.pay("cy", "bob", 501), 409, "insufficient_funds")
        self.assertEqual(self.pay("cy", "bob", 500)[0], 201)
        self.assertEqual(self.bal("cy"), 0)

    def test_amount_forms(self):
        for good in (1000, 1000.0, 1e3):
            self.assertEqual(self.pay("ada", "bob", good)[0], 201)
        for bad in (0, -1, 1.5, "100", True, None, 1_000_000_001, [1]):
            self.assertErr(self.pay("ada", "bob", bad), 422, "validation_failed")

    def test_note_and_visibility_rules(self):
        note = "café \U0001F600  spaces "
        s, p = self.pay("ada", "bob", 1, note=note)
        self.assertEqual(p["note"], note)
        self.assertEqual(self.pay("ada", "bob", 1, note="\U0001F600" * 200)[0], 201)
        self.assertErr(self.pay("ada", "bob", 1, note="x" * 201), 422, "validation_failed")
        self.assertErr(self.pay("ada", "bob", 1, note=None), 422, "validation_failed")
        self.assertErr(self.pay("ada", "bob", 1, visibility="friends"), 422, "validation_failed")
        self.assertErr(self.pay("ada", "ada", 1), 422, "self_payment")
        self.assertErr(self.pay("ada", "nobody", 1), 404, "not_found")

    def test_malformed_and_auth(self):
        self.assertErr(call("POST", "/payments", raw=b"{nope", token=self.tok["ada"], key="k"),
                       400, "malformed_request")
        self.assertErr(call("POST", "/payments", {"to_handle": "bob", "amount": 1}, key="k"),
                       401, "unauthenticated")
        self.assertErr(call("POST", "/payments", {"to_handle": "bob", "amount": 1},
                            token=self.tok["ada"]), 400, "missing_idempotency_key")
        self.assertErr(call("POST", "/payments", {"to_handle": "bob", "amount": 1},
                            token=self.tok["ada"], key="x" * 256), 422, "validation_failed")
        self.assertErr(call("GET", "/me", token="bogus"), 401, "unauthenticated")


class Idempotency(Base):
    def test_replay_reuse_and_failed_key(self):
        k = new_key()
        s1, b1 = self.pay("ada", "bob", 100, key=k)
        s2, b2 = self.pay("ada", "bob", 100, key=k)
        self.assertEqual((s1, s2), (201, 200))
        self.assertEqual(b1, b2)
        self.assertErr(self.pay("ada", "bob", 200, key=k), 409, "idempotency_key_reuse")
        self.assertErr(self.pay("ada", "bob", -5, key=k), 409, "idempotency_key_reuse")
        self.assertEqual(self.bal("ada"), 9900)
        # same key, different user: independent
        self.assertEqual(self.pay("bob", "ada", 100, key=k)[0], 201)
        # failed key is reusable
        k2 = new_key()
        self.assertErr(self.pay("cy", "bob", 9999, key=k2), 409, "insufficient_funds")
        self.assertEqual(self.pay("cy", "bob", 10, key=k2)[0], 201)

    def test_same_key_other_path_is_not_replay(self):
        k = new_key()
        self.assertEqual(call("POST", "/requests", {"payer_handle": "bob", "amount": 5},
                              token=self.tok["ada"], key=k)[0], 201)
        self.assertEqual(call("POST", "/splits", {"amount": 5, "participant_handles": ["bob"]},
                              token=self.tok["ada"], key=k)[0], 201)

    def test_concurrent_duplicates_take_effect_once(self):
        k = new_key()
        with ThreadPoolExecutor(30) as pool:
            results = list(pool.map(lambda _: self.pay("ada", "bob", 100, key=k), range(30)))
        self.assertEqual(sorted(s for s, _ in results).count(201), 1)
        self.assertTrue(all(s in (200, 201) for s, _ in results))
        self.assertEqual(len({json.dumps(b, sort_keys=True) for _, b in results}), 1)
        self.assertEqual(self.bal("ada"), 9900)

    def test_concurrent_transfers_never_overdraw(self):
        def hit(i):
            return self.pay("cy", "bob" if i % 2 else "ada", 30)
        with ThreadPoolExecutor(50) as pool:
            results = list(pool.map(hit, range(50)))
        ok = sum(1 for s, _ in results if s == 201)
        self.assertEqual(ok, 16)  # 500 // 30
        self.assertTrue(all(s in (201, 409) for s, _ in results))
        self.assertEqual(self.bal("cy"), 500 - 16 * 30)
        self.assertEqual(self.total(), 13000)


class Requests(Base):
    def ask(self, frm, payer, amount, key=None, **extra):
        return call("POST", "/requests", {"payer_handle": payer, "amount": amount, **extra},
                    token=self.tok[frm], key=key or new_key())

    def test_request_lifecycle(self):
        s, rq = self.ask("bob", "cy", 1200, note="taxi")
        self.assertEqual((s, rq["status"], rq["payment_id"]), (201, "pending", None))
        rid = rq["request_id"]
        self.assertErr(call("POST", f"/requests/{rid}/pay", {}, token=self.tok["cy"], key=new_key()),
                       409, "insufficient_funds")
        self.assertErr(call("POST", f"/requests/{rid}/pay", {}, token=self.tok["bob"], key=new_key()),
                       403, "forbidden")
        self.pay("ada", "cy", 1000)
        k = new_key()
        s, p = call("POST", f"/requests/{rid}/pay", {"visibility": "private"},
                    token=self.tok["cy"], key=k)
        self.assertEqual((s, p["request_id"], p["visibility"], p["amount"]), (201, rid, "private", 1200))
        s2, p2 = call("POST", f"/requests/{rid}/pay", {"visibility": "private"},
                      token=self.tok["cy"], key=k)
        self.assertEqual((s2, p2), (200, p))
        self.assertErr(call("POST", f"/requests/{rid}/pay", {}, token=self.tok["cy"], key=k),
                       409, "idempotency_key_reuse")
        self.assertErr(call("POST", f"/requests/{rid}/pay", {"visibility": "private"},
                            token=self.tok["cy"], key=new_key()), 409, "request_not_pending")
        self.assertEqual(self.bal("cy"), 300)
        self.assertErr(call("POST", f"/requests/{rid}/decline", token=self.tok["cy"]),
                       409, "request_not_pending")

    def test_decline_cancel_idempotent_and_permissions(self):
        rid = self.ask("bob", "ada", 10)[1]["request_id"]
        self.assertErr(call("POST", f"/requests/{rid}/decline", token=self.tok["bob"]), 403, "forbidden")
        self.assertErr(call("POST", f"/requests/{rid}/cancel", token=self.tok["ada"]), 403, "forbidden")
        self.assertEqual(call("POST", f"/requests/{rid}/decline", token=self.tok["ada"])[1]["status"], "declined")
        self.assertEqual(call("POST", f"/requests/{rid}/decline", token=self.tok["ada"])[0], 200)
        self.assertErr(call("POST", f"/requests/{rid}/cancel", token=self.tok["bob"]), 409, "request_not_pending")
        self.assertErr(call("POST", f"/requests/{rid}/decline", token=self.tok["cy"]), 403, "forbidden")
        self.assertErr(call("POST", f"/requests/{rid}/pay", {}, token=self.tok["cy"], key=new_key()),
                       403, "forbidden")
        self.assertErr(call("POST", "/requests/nope/decline", token=self.tok["cy"]), 404, "not_found")

    def test_listing_filters_and_pagination(self):
        for i in range(5):
            self.ask("bob", "ada", 10 + i)
        self.ask("ada", "bob", 1)
        self.ask("cy", "bob", 1)
        s, body = call("GET", "/requests?direction=incoming&limit=2", token=self.tok["ada"])
        self.assertEqual([r["amount"] for r in body["requests"]], [14, 13])
        self.assertTrue(body["has_more"])
        s, body = call("GET", "/requests?direction=outgoing", token=self.tok["ada"])
        self.assertEqual(len(body["requests"]), 1)
        s, body = call("GET", "/requests", token=self.tok["ada"])
        self.assertEqual(len(body["requests"]), 6)
        for q in ("limit=0", "limit=201", "limit=1e1", "limit=+4", "offset=-1", "direction=x", "status=x"):
            self.assertErr(call("GET", f"/requests?{q}", token=self.tok["ada"]), 422, "validation_failed")


class Splits(Base):
    def split(self, frm, amount, handles, key=None, **extra):
        return call("POST", "/splits", {"amount": amount, "participant_handles": handles, **extra},
                    token=self.tok[frm], key=key or new_key())

    def test_shares_and_requests(self):
        for amount, expected in ((1000, [334, 333, 333]), (1, [1, 0, 0]), (10, [4, 3, 3]),
                                 (999, [333, 333, 333])):
            s, body = self.split("ada", amount, ["ada", "bob", "cy"])
            self.assertEqual([x["amount"] for x in body["shares"]], expected)
            self.assertEqual([r["payer_handle"] for r in body["requests"]], ["bob", "cy"])
            self.assertEqual([r["amount"] for r in body["requests"]], expected[1:])
        s, body = self.split("ada", 1000, ["cy", "bob", "ada"])
        self.assertEqual(body["shares"][0], {"handle": "cy", "amount": 334})
        s, body = self.split("ada", 999, ["ada"])
        self.assertEqual((s, body["requests"]), (201, []))
        s, body = self.split("ada", 1000, ["bob", "cy"])
        self.assertEqual([r["amount"] for r in body["requests"]], [500, 500])

    def test_split_errors(self):
        self.assertErr(self.split("ada", 10, []), 422, "validation_failed")
        self.assertErr(self.split("ada", 10, ["bob", "bob"]), 422, "validation_failed")
        self.assertErr(self.split("ada", 10, ["bob", "nobody"]), 404, "not_found")
        self.assertErr(self.split("ada", 0, ["bob"]), 422, "validation_failed")


class Feed(Base):
    def test_visibility(self):
        self.pay("ada", "bob", 1, visibility="private")
        self.pay("bob", "cy", 2)
        ids = lambda h: [p["amount"] for p in call("GET", "/activity", token=self.tok[h])[1]["payments"]]
        self.assertEqual(ids("ada"), [2, 1])
        self.assertEqual(ids("bob"), [2, 1])
        self.assertEqual(ids("cy"), [2])


class Settlements(Base):
    def setUp(self):
        self.reset(fixture(settlement_operator_ids=["u_ada"]))

    def settle(self, transfers, token=None, key=None):
        return call("POST", "/settlements", {"transfers": transfers},
                    token=token or self.tok["ada"], key=key or new_key())

    def test_net_affordability_and_atomicity(self):
        # cy only has 500 but receives 400 first in the batch net
        s, body = self.settle([{"from_handle": "bob", "to_handle": "cy", "amount": 400},
                               {"from_handle": "cy", "to_handle": "ada", "amount": 900,
                                "visibility": "private"}])
        self.assertEqual(s, 201, body)
        self.assertEqual(len({p["created_at"] for p in body["payments"]} | {body["committed_at"]}), 1)
        self.assertTrue(all(p["settlement_id"] == body["settlement_id"] for p in body["payments"]))
        self.assertEqual((self.bal("bob"), self.bal("cy"), self.bal("ada")), (2100, 0, 10900))
        before = [self.bal(h) for h in ("ada", "bob", "cy")]
        self.assertErr(self.settle([{"from_handle": "bob", "to_handle": "cy", "amount": 1},
                                    {"from_handle": "cy", "to_handle": "ada", "amount": 2}]),
                       409, "insufficient_funds")
        self.assertEqual([self.bal(h) for h in ("ada", "bob", "cy")], before)

    def test_permissions_and_entry_errors(self):
        self.assertErr(self.settle([{"from_handle": "bob", "to_handle": "cy", "amount": 1}],
                                   token=self.tok["bob"]), 403, "forbidden")
        self.assertErr(call("POST", "/settlements", {"transfers": []}, key="k"), 401, "unauthenticated")
        self.assertErr(self.settle([]), 422, "validation_failed")
        self.assertErr(self.settle([{"from_handle": "bob", "to_handle": "cy", "amount": 1}] * 33),
                       422, "validation_failed")
        self.assertErr(self.settle([{"from_handle": "bob", "to_handle": "nobody", "amount": 1},
                                    {"from_handle": "bob", "to_handle": "bob", "amount": 1}]),
                       404, "not_found")
        self.assertErr(self.settle([{"from_handle": "bob", "to_handle": "bob", "amount": 1},
                                    {"from_handle": "bob", "to_handle": "nobody", "amount": 1}]),
                       422, "self_payment")
        self.assertErr(self.settle([{"from_handle": "cy", "to_handle": "bob", "amount": 10 ** 9},
                                    {"from_handle": "bob", "to_handle": "cy", "amount": 0}]),
                       422, "validation_failed")

    def test_operator_cannot_read_private_items(self):
        self.settle([{"from_handle": "bob", "to_handle": "cy", "amount": 1, "visibility": "private"}])
        self.assertEqual(call("GET", "/activity", token=self.tok["ada"])[1]["payments"], [])


class Lifecycle(Base):
    def test_signup_and_derived_handle(self):
        s, body = call("POST", "/auth/signup", {"email": "Dee.Dee+x@Example.com", "password": "longenough",
                                                "display_name": "Dee"})
        self.assertEqual(s, 201, body)
        me = call("GET", "/me", token=body["token"])[1]
        self.assertEqual((me["handle"], me["balance"]), ("dee_dee_x", 0))
        self.assertErr(call("POST", "/auth/signup", {"email": "dee.dee+x@example.com", "password": "longenough",
                                                     "display_name": "D"}), 409, "email_taken")
        self.assertErr(call("POST", "/auth/signup", {"email": "ada@other.org", "password": "longenough",
                                                     "display_name": "A"}), 409, "handle_taken")
        self.assertErr(call("POST", "/auth/login", {"email": "ada@other.org", "password": "longenough"}),
                       401, "unauthenticated")
        self.assertErr(call("POST", "/auth/signup", {"email": "x@y", "password": "short",
                                                     "display_name": "X"}), 422, "validation_failed")
        self.assertErr(call("POST", "/auth/signup", {"email": "nope", "password": "longenough",
                                                     "display_name": "X"}), 422, "validation_failed")

    def test_caller_removed_by_concurrent_reset_is_401_not_5xx(self):
        # Simulates a reset landing between authentication and the write.
        stale = app.STORE.authenticate(self.tok["ada"])
        self.reset(fixture(users=[user("zed", 5), user("bob", 1)]))
        for action in (lambda: app.STORE.me(stale),
                       lambda: app.STORE.create_payment(stale, {"to_handle": "bob", "amount": 1}),
                       lambda: app.STORE.create_request(stale, {"payer_handle": "bob", "amount": 1}),
                       lambda: app.STORE.create_split(stale, {"amount": 1, "participant_handles": ["bob"]})):
            with self.assertRaises(app.ApiError) as ctx:
                action()
            self.assertEqual((ctx.exception.status, ctx.exception.code), (401, "unauthenticated"))

    def test_negative_fixture_balance_changes_nothing(self):
        self.pay("ada", "bob", 7)
        bad = fixture(users=[user("zed", -1)])
        self.assertErr(call("POST", "/_test/reset", bad), 422, "validation_failed")
        self.assertEqual(self.bal("ada"), 9993)

    def test_export_import_round_trip(self):
        k = new_key()
        s, original = self.pay("ada", "bob", 100, key=k)
        s, export = call("GET", "/_test/export")
        self.assertEqual((s, export["track"], export["format_version"]), (200, "pocketful", 1))
        self.pay("ada", "bob", 1)  # after export: must not leak into the snapshot
        self.reset(fixture(users=[user("zed", 5)]))
        self.assertEqual(call("POST", "/_test/import", export)[0], 204)
        self.assertEqual(call("POST", "/_test/import", export)[0], 204)
        # old tokens still valid, replay still returns the original receipt
        ada_token = [t for t, uid in export["state"]["tokens"].items() if uid == "u_ada"][0]
        s, replay = call("POST", "/payments", {"to_handle": "bob", "amount": 100},
                         token=ada_token, key=k)
        self.assertEqual((s, replay), (200, original))
        self.assertEqual(call("GET", "/me", token=ada_token)[1]["balance"], 9900)
        self.assertErr(call("POST", "/auth/login", {"email": "zed@example.com", "password": PW}),
                       401, "unauthenticated")
        s, _ = call("POST", "/auth/login", {"email": "ada@example.com", "password": PW})
        self.assertEqual(s, 200)
        for bad in ({}, {**export, "track": "other"}, {**export, "format_version": 2},
                    {**export, "state": {"users": 1}}):
            self.assertErr(call("POST", "/_test/import", bad), 422, "validation_failed")
        self.assertEqual(call("GET", "/me", token=ada_token)[1]["balance"], 9900)

    def test_seeded_state(self):
        fx = fixture(payments=[{"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob",
                                "amount": 500, "note": "coffee", "visibility": "public"}],
                     requests=[{"id": "rq_1", "requester_id": "u_bob", "payer_id": "u_ada",
                                "amount": 1200, "note": "taxi", "status": "pending"}])
        self.reset(fx)
        self.assertEqual(self.bal("ada"), 10000)
        s, p = self.pay("ada", "bob", 1)
        self.assertNotEqual(p["payment_id"], "p_1")
        feed = call("GET", "/activity", token=self.tok["cy"])[1]["payments"]
        self.assertEqual([x["payment_id"] for x in feed][-1], "p_1")
        s, p = call("POST", "/requests/rq_1/pay", {}, token=self.tok["ada"], key=new_key())
        self.assertEqual((s, p["request_id"]), (201, "rq_1"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
