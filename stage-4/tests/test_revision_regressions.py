"""Additional domain-level Stage 3/4 atomicity and upgrade regressions.

Run: python3 -m unittest discover -s tests -p test_revision_regressions.py
"""
import copy
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wallet import Store, ApiError

T0 = "2020-01-01T00:00:00+00:00"
T1 = "2020-01-02T00:00:00+00:00"
T2 = "2020-01-03T00:00:00+00:00"


class RevisionRegressions(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.reset({"users": [
            {"id": name, "handle": name, "email": name + "@example.com", "password": "correct horse",
             "balance": balance} for name, balance in (("ada", 500), ("bob", 500), ("cy", 0))],
            "settlement_operator_ids": ["cy"], "payments": [
                {"id": "income", "from_user_id": "bob", "to_user_id": "ada", "amount": 1000, "created_at": T1},
                {"id": "outgoing", "from_user_id": "ada", "to_user_id": "bob", "amount": 500, "created_at": T2}]})
        self.users = self.store.state["users"]

    def item(self, pid="outgoing", amount=500, effective=T2, revision=1):
        return {"payment_id": pid, "amount": amount, "effective_at": effective,
                "expected_revision": revision, "reason": "repair"}

    def batch(self, items, key="batch"):
        body = {"corrections": items}
        return self.store.idempotent("cy", "POST", "/correction-batches", key, body,
            lambda: self.store.create_correction_batch(self.users["cy"], body))

    def test_rejected_batch_preserves_exact_export(self):
        before = self.store.export()
        with self.assertRaises(ApiError) as ctx:
            self.batch([self.item(effective=T0)])
        self.assertEqual(ctx.exception.code, "historical_overdraft")
        self.assertEqual(self.store.export(), before)
        self.assertEqual(self.batch([self.item()])[0], 201)

    def test_snapshots_survive_export_import_and_remain_caller_bound(self):
        old = self.store.statement(self.users["ada"], {}, 50, 0)
        self.batch([self.item(amount=400)])
        document = self.store.export()
        other = Store()
        other.import_(document)
        self.assertEqual(other.statement(other.state["users"]["ada"],
                         {"snapshot": [old["snapshot"]]}, 50, 0), old)
        with self.assertRaises(ApiError) as ctx:
            other.statement(other.state["users"]["bob"], {"snapshot": [old["snapshot"]]}, 50, 0)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_concurrent_same_revision_only_once_and_exact_replay(self):
        with ThreadPoolExecutor(16) as pool:
            results = list(pool.map(lambda _: self.batch([self.item(amount=400)]), range(32)))
        self.assertEqual(sum(s == 201 for s, _ in results), 1)
        self.assertEqual(sum(s == 200 for s, _ in results), 31)
        first = results[0][1]
        self.batch([self.item(amount=300, revision=2)], key="next")
        self.assertEqual(self.batch([self.item(amount=400)]), (200, first))
        self.assertEqual(sum(u["balance"] for u in self.users.values()), 1000)

    def test_refund_caps_use_corrected_amount_and_no_refund_corrections(self):
        self.batch([self.item(amount=400)])
        refund = self.store.refund_payment(self.users["bob"], "outgoing", {"amount": 250})
        self.assertEqual(refund["refund_of"], "outgoing")
        with self.assertRaises(ApiError) as ctx:
            self.store.refund_payment(self.users["bob"], "outgoing", {"amount": 151})
        self.assertEqual(ctx.exception.code, "refund_exceeds_payment")
        before = self.store.export()
        with self.assertRaises(ApiError) as ctx:
            self.batch([self.item(amount=249, revision=2)], key="below")
        self.assertEqual(ctx.exception.code, "refund_exceeds_payment")
        self.assertEqual(before, self.store.export())
        with self.assertRaises(ApiError) as ctx:
            self.batch([self.item(pid=refund["payment_id"], amount=0)], key="linked")
        self.assertEqual(ctx.exception.code, "linked_payment_immutable")

    def test_input_error_precedes_settlement_completeness(self):
        receipt = self.store.create_settlement(self.users["cy"], {"transfers": [
            {"from_handle": "ada", "to_handle": "bob", "amount": 10},
            {"from_handle": "bob", "to_handle": "ada", "amount": 10}]})
        member = receipt["payments"][0]
        with self.assertRaises(ApiError) as ctx:
            self.batch([self.item(pid=member["payment_id"], amount=10, effective=member["created_at"]),
                        self.item(pid="missing")])
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(ApiError) as ctx:
            self.batch([self.item(pid=member["payment_id"], amount=10, effective=member["created_at"])])
        self.assertEqual(ctx.exception.code, "incomplete_settlement")

    def test_equal_instant_settlement_netting(self):
        receipt = self.store.create_settlement(self.users["cy"], {"transfers": [
            {"from_handle": "ada", "to_handle": "bob", "amount": 500},
            {"from_handle": "bob", "to_handle": "ada", "amount": 500}]})
        items = [self.item(pid=p["payment_id"], amount=1000, effective=p["created_at"])
                 for p in receipt["payments"]]
        self.assertEqual(self.batch(items)[0], 201)
        self.assertEqual((self.users["ada"]["balance"], self.users["bob"]["balance"]), (500, 500))


if __name__ == "__main__":
    unittest.main()
