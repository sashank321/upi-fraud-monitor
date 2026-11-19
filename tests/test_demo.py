from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from investigation import investigate
from payments import PaymentError, SCENARIOS, Store


class DemoTest(unittest.TestCase):
    def setUp(self):
        data = Path(__file__).resolve().parents[1] / "data"
        data.mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=data)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.sqlite3"
        self.store = Store(self.path)

    def submit(self, index):
        item = SCENARIOS[index]
        transfer_id, created = self.store.transfer(item["payee"], item["amount"], item["id"])
        self.assertTrue(created)
        return transfer_id

    def test_three_outcomes_and_balanced_journal_survive_restart(self):
        routine, suspicious, rent = [self.submit(index) for index in range(3)]
        with self.assertRaises(PaymentError):
            self.store.resolve(rent, "release", "Pending confirmation", False)
        self.store.resolve(suspicious, "cancel", "Payer withdrew the proposal", False)
        self.store.resolve(rent, "release", "Recipient and amount confirmed", True)

        snapshot = Store(self.path).snapshot()
        self.assertEqual({row["id"]: row["status"] for row in snapshot["transfers"]},
                         {routine: "settled", suspicious: "cancelled", rent: "settled"})
        accounts = {account["id"]: account for account in snapshot["accounts"]}
        self.assertEqual(accounts["demo-payer"]["available"], 10000000 - 45000 - 1800000)
        self.assertEqual(accounts["demo-payer"]["reserved"], 0)
        self.assertEqual(accounts["new-recipient"]["available"], 0)
        self.assertEqual(accounts["rent"]["available"], 1800000)
        self.assertEqual(accounts["canteen"]["available"], 45000)
        grouped = {}
        for row in snapshot["journal"]:
            key = row["transfer_id"], row["operation"]
            grouped[key] = grouped.get(key, 0) + row["delta"]
        self.assertTrue(all(total == 0 for total in grouped.values()))
        self.assertEqual(len(grouped), 5)

    def test_idempotency_and_concurrent_reviews(self):
        def send(_):
            return self.store.transfer("rent", 1800000, "same-request")

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(send, range(4)))
            self.assertEqual(sum(created for _, created in results), 1)
            self.assertEqual(len({transfer_id for transfer_id, _ in results}), 1)
            transfer_id = results[0][0]
            with self.assertRaises(PaymentError) as conflict:
                self.store.transfer("rent", 1800001, "same-request")
            self.assertEqual(conflict.exception.status, 409)

            def review(action):
                try:
                    self.store.resolve(transfer_id, action, "Concurrent decision", True)
                    return 200
                except PaymentError as error:
                    return error.status

            self.assertCountEqual(pool.map(review, ["release", "cancel"]), [200, 409])
        self.assertEqual(len(self.store.snapshot()["journal"]), 4)

    def test_expiry_refunds_and_invalidates_in_flight_investigation(self):
        transfer_id = self.submit(1)
        _, token, amount, evidence = self.store.claim()
        self.store.record_tool(transfer_id, token, "sender_history")
        with self.store.transaction() as db:
            db.execute("UPDATE transfers SET expires_at = 0 WHERE id = ?", (transfer_id,))
        with self.assertRaises(PaymentError) as conflict:
            self.store.resolve(transfer_id, "release", "Late approval", True)
        self.assertEqual(conflict.exception.status, 409)
        self.store.complete(transfer_id, token, investigate(amount, evidence, None))
        self.store.record_tool(transfer_id, token, "recipient_activity")
        self.store.expire()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["transfers"][0]["status"], "expired")
        self.assertEqual(snapshot["transfers"][0]["investigation_status"], "closed")
        self.assertIsNone(snapshot["transfers"][0]["report"])
        self.assertEqual([step["tool"] for step in snapshot["transfers"][0]["investigation_steps"]],
                         ["sender_history"])
        payer = next(account for account in snapshot["accounts"] if account["id"] == "demo-payer")
        self.assertEqual(payer["available"], 10000000)
        self.assertEqual(len(snapshot["journal"]), 4)

    def test_worker_reclaims_abandoned_job_and_persists_validated_advice(self):
        self.submit(1)
        transfer_id, abandoned_token, amount, evidence = self.store.claim()
        with self.store.transaction() as db:
            db.execute("UPDATE investigations SET leased_until = 0")
        _, token, _, _ = Store(self.path).claim()
        choices = iter([
            {"tool": "recipient_activity"},
            {"tool": "verification_guidance"},
            {"action": "request_verification", "summary": "Independently verify the recipient.",
             "evidence": ["recipient_activity", "verification_guidance"]},
        ])

        def choose(context, timeout):
            result = next(choices)
            if "tool" in result:
                self.assertIn(result["tool"], context["available_tools"])
            else:
                self.assertEqual(context["results"]["recipient_activity"]["distinct_senders"], 7)
            return result

        report = investigate(amount, evidence, choose,
                             lambda tool: self.store.record_tool(transfer_id, token, tool))
        self.assertEqual(report["mode"], "agent")
        self.assertEqual(report["trace"], ["recipient_activity", "verification_guidance"])
        self.store.complete(transfer_id, abandoned_token, investigate(amount, evidence, None))
        self.store.complete(transfer_id, token, report)
        self.assertEqual(self.store.snapshot()["transfers"][0]["report"], report)
        self.assertEqual([step["tool"] for step in self.store.snapshot()["transfers"][0]["investigation_steps"]],
                         report["trace"])
        self.assertEqual(self.store.snapshot()["transfers"][0]["status"], "held")

    def test_model_output_outside_allowlist_goes_to_manual_review(self):
        self.submit(2)
        _, _, amount, evidence = self.store.claim()
        report = investigate(amount, evidence, lambda context, timeout: {"tool": "release_payment"})
        self.assertEqual(report["mode"], "manual")
        self.assertEqual(report["action"], "escalate")
        self.assertEqual(report["trace"], [])

    def test_historical_role_views(self):
        self.submit(0)
        self.submit(1)
        self.submit(2)
        bank = self.store.view("bank")
        cases = {item["payee"]: item for item in bank["transfers"]}
        self.assertEqual(cases["canteen"]["status"], "settled")
        self.assertEqual(cases["new-recipient"]["status"], "held")
        self.assertEqual(cases["rent"]["status"], "held")
        payer = self.store.view("sender")
        self.assertEqual(len(payer["transfers"]), 3)
        receiver = self.store.view("receiver", "rent")
        self.assertEqual(len(receiver["transfers"]), 1)
        self.assertEqual(receiver["account"]["available"], 0)
        self.assertNotIn("risk", payer["transfers"][0])
        self.assertNotIn("report", receiver["transfers"][0])
        self.assertNotIn("investigation_steps", receiver["transfers"][0])
        self.assertNotIn("journal", receiver)

    def test_legacy_hold_keeps_funds_reserved(self):
        self.submit(0)
        transfer_id, _ = self.store.transfer("canteen", 4500300, "canteen-outlier")
        case = next(row for row in self.store.view("bank")["transfers"] if row["id"] == transfer_id)
        self.assertEqual(case["status"], "held")
        self.assertTrue(any("five times the sender" in reason for reason in case["reasons"]))
        self.assertEqual(self.store.view("sender")["account"]["reserved"], 4500300)
        self.assertEqual(self.store.view("receiver", "canteen")["account"]["available"], 45000)
