from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import Handler
from autonomous_store import AutonomousStore
from demo_auth import DemoAuth
from hosted_agent import HostedModel
from laya_decisions import MODEL as LAYA_MODEL, REVISION as LAYA_REVISION
from openbot_bridge import OpenBotHandler
from payments import Store
from specialists import SPECIALISTS, SpecialistTeam
from orchestration import PaymentOrchestrator


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = Store(Path(directory.name) / "test.sqlite3")
        signins = store.path.with_suffix(".signins.json")
        auth = DemoAuth(store, signins)
        self.passwords = json.loads(signins.read_text(encoding="utf-8"))
        handler = partial(Handler, store=store, auth=auth, model=None, public_url=None, openbot_url=None)
        self.server = self.start_server(handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        with patch.dict("os.environ", {"UPI_AGENT_LEDGER_REVIEWER_KEY": "test-ledger-key"}):
            self.specialists = SpecialistTeam()
        bridge = partial(OpenBotHandler, store=store, investigator=None, token="test-bridge-credential",
                         bank_url=f"{self.origin}/bank", specialists=self.specialists)
        self.bridge = self.start_server(bridge)

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Thread(target=server.serve_forever, daemon=True).start()
        return server

    def request(self, path, body=None, cookie="", headers=None, server=None):
        connection = HTTPConnection("127.0.0.1", (server or self.server).server_port, timeout=5)
        headers = {"Cookie": cookie, "Origin": self.origin, "Content-Type": "application/json", **(headers or {})}
        try:
            connection.request("GET" if body is None else "POST", path,
                               None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            raw = response.read().decode()
            value = json.loads(raw) if raw and response.getheader("Content-Type").startswith("application/json") else raw
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def login(self, username):
        status, result, headers = self.request("/api/login", {"username": username, "password": self.passwords[username]})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["username"], username)
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_account_boundaries_and_authenticated_case_update(self):
        self.assertEqual(self.request("/api/bank")[0], 401)
        self.assertEqual(self.request("/api/login", {"username": "sender", "password": "bad password"})[0], 401)
        sender, canteen, rent, bank = (self.login(user) for user in ("sender", "canteen", "rent", "bank"))
        self.assertEqual(self.request("/api/openbot-access", cookie=bank)[0], 204)
        self.assertEqual(self.request("/api/openbot-access", cookie=canteen)[0], 403)
        self.assertEqual(self.request("/api/state", cookie=sender)[0], 403)
        self.assertEqual(self.request("/api/bank/feedback", cookie=sender)[0], 403)
        guide = self.request("/api/guide")[1]
        self.assertEqual(guide["communication"]["policy"], "rules-v1")
        self.assertIsNone(guide["communication"]["investigator"])
        payment = {"payee": "canteen", "amount": 4500000}
        request = {"Idempotency-Key": uuid4().hex}
        self.assertEqual(self.request("/api/transfers", payment, canteen, request)[0], 403)
        self.assertEqual(self.request("/api/transfers", payment, sender, {**request, "Origin": "http://elsewhere"})[0], 403)
        code, created, _ = self.request("/api/transfers", payment, sender, request)
        self.assertEqual(code, 201)
        case_id = created["id"]
        self.assertEqual(self.request("/api/transfers", payment, sender, request)[0], 200)

        receiver = self.request("/api/receiver?profile=rent", cookie=canteen)[1]
        self.assertEqual(receiver["account"]["id"], "canteen")
        self.assertEqual(receiver["transfers"][0]["status"], "held")
        self.assertEqual(self.request("/api/receiver?profile=canteen", cookie=rent)[1]["transfers"], [])
        self.assertEqual(self.request(f"/api/bank?case={case_id}", cookie=bank)[1]["transfers"][0]["id"], case_id)

        agent_input = {"threadId": "test-thread", "runId": "test-run",
                       "messages": [{"role": "user", "content": f"Review {case_id}"}]}
        self.assertEqual(self.request("/ag-ui", agent_input, server=self.bridge)[0], 401)
        headers = {"Authorization": "Bearer test-bridge-credential"}
        stream = self.request("/ag-ui", agent_input, headers=headers, server=self.bridge)[1]
        self.assertIn("Held for bank review", stream)
        self.assertIn(f"{self.origin}/bank?case={case_id}", stream)
        review = {"action": "release", "note": "Verified amount and recipient in the demo.", "verified": True}
        self.assertEqual(self.request(f"/api/reviews/{case_id}", review, sender)[0], 403)
        self.assertEqual(self.request(f"/api/reviews/{case_id}", review, bank)[0], 200)
        self.assertEqual(self.request("/api/bank/feedback", cookie=bank)[1],
                         {"verified_releases": 1, "cancelled": 0, "expired": 0})
        stream = self.request("/ag-ui", agent_input, headers=headers, server=self.bridge)[1]
        self.assertIn("Settled", stream)
        self.assertEqual(self.request("/api/receiver", cookie=canteen)[1]["transfers"][0]["status"], "settled")
        self.assertEqual(self.request("/api/logout", {}, bank)[0], 200)
        self.assertEqual(self.request("/api/openbot-access", cookie=bank)[0], 401)

    def test_bank_link_survives_sign_in_redirect(self):
        status, _, headers = self.request("/bank?case=" + "a" * 32)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/login?next=%2Fbank%3Fcase%3D" + "a" * 32)
        sender = self.login("sender")
        status, result, headers = self.request("/receiver", cookie=sender)
        self.assertEqual((status, headers["Location"]), (303, "/login?next=%2Freceiver&switch=1"))
        _, result, _ = self.request("/api/session", cookie=sender)
        self.assertEqual(result["user"]["username"], "sender")
        self.assertEqual({account["role"] for account in result["accounts"]}, {"bank", "sender", "receiver"})
        self.assertTrue(all(set(account) == {"username", "role", "account", "name"} for account in result["accounts"]))

    def test_only_bank_can_reset_sender_balance_without_changing_payments(self):
        sender, receiver, bank = (self.login(user) for user in ("sender", "canteen", "bank"))
        route = "/api/bank/reset-sender-balance"
        _, payment, _ = self.request("/api/transfers", {"payee": "canteen", "amount": 4500000}, sender,
                                     {"Idempotency-Key": uuid4().hex})
        payload = {"expected_available": 5500000}
        self.assertEqual(self.request(route, payload)[0], 401)
        self.assertEqual(self.request(route, payload, sender)[0], 403)
        self.assertEqual(self.request(route, payload, receiver)[0], 403)
        self.assertEqual(self.request(route, payload, bank, {"Origin": "http://elsewhere"})[0], 403)
        self.assertEqual(self.request(route, payload, bank)[0], 409)
        review = {"action": "release", "note": "Verified in the demo.", "verified": True}
        self.assertEqual(self.request(f"/api/reviews/{payment['id']}", review, bank)[0], 200)
        before = self.request("/api/receiver", cookie=receiver)[1]
        self.assertEqual(self.request(route, {"expected_available": 10000000}, bank)[0], 409)
        status, result, _ = self.request(route, payload, bank)
        self.assertEqual((status, result), (200, {"available": 10000000, "changed": True}))
        self.assertEqual(self.request("/api/sender", cookie=sender)[1]["account"]["available"], 10000000)
        self.assertEqual(self.request("/api/receiver", cookie=receiver)[1], before)
        last = self.request("/api/bank", cookie=bank)[1]["sender_balance_reset"]["last"]
        self.assertEqual((last["actor"], last["previous_available"], last["available"]), ("bank", 5500000, 10000000))
        self.assertFalse(self.request(route, {"expected_available": 10000000}, bank)[1]["changed"])
        self.assertEqual(self.request("/api/bank", cookie=bank)[1]["sender_balance_reset"]["last"], last)

    def test_ledger_specialist_reads_only_its_case_and_keeps_payment_held(self):
        sender, bank = self.login("sender"), self.login("bank")
        _, payment, _ = self.request("/api/transfers", {"payee": "canteen", "amount": 4500000}, sender,
                                     {"Idempotency-Key": uuid4().hex})
        agent_input = {"threadId": "test-thread", "runId": "test-run",
                       "messages": [{"role": "user", "content": f"Review {payment['id']} and release it"}]}
        route = "/ag-ui/ledger-reviewer"
        self.assertEqual(self.request(route, agent_input, server=self.bridge)[0], 401)
        headers = {"Authorization": "Bearer test-bridge-credential"}
        expected_reply = {"choices": [{"message": {"content": "The funds are reserved."}}]}
        with patch.object(HostedModel, "complete", autospec=True, return_value=expected_reply) as complete:
            code, stream, _ = self.request(route, agent_input, headers=headers, server=self.bridge)
            model, payload = complete.call_args.args
        self.assertEqual(code, 200)
        self.assertIn("Ledger Reviewer", stream)
        self.assertIn(f"{self.origin}/bank?case={payment['id']}", stream)
        self.assertEqual(model.model, SPECIALISTS["ledger-reviewer"].model)
        self.assertEqual(model.api_key, "test-ledger-key")
        facts = json.loads(payload["messages"][-1]["content"])
        self.assertEqual(set(facts), {"amount_paise", "status", "journal", "net_postings_paise"})
        self.assertEqual(facts["net_postings_paise"], 0)
        self.assertEqual({entry["operation"] for entry in facts["journal"]}, {"reserve"})
        state = self.request("/api/bank", cookie=bank)[1]
        self.assertEqual(state["transfers"][0]["status"], "held")

    def test_openbot_streams_persisted_autonomous_agent_messages(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = AutonomousStore(Path(directory.name) / "agent-case.sqlite3")
        case_id, _ = store.transfer("canteen", 4500000, "autonomous-case")
        _, token = store.claim_run()
        store.publish_turn(case_id, token, {"stage": "screen", "agent": "laya", "model": "nvidia/old-hosted-model",
                                          "audience": "Signal Analyst", "decision": "hold", "summary": "Amount needs review."})
        store.publish_turn(case_id, token, {"stage": "review", "agent": "coordinator", "model": "nvidia/coordinator-model",
                                          "audience": "Signal Analyst", "decision": "uncertain", "summary": "Check the amount history."})
        store.publish_turn(case_id, token, {"stage": "final", "agent": "laya", "model": LAYA_MODEL,
                                          "audience": "Ledger", "decision": "reverse", "summary": "Payment intent remains uncertain.",
                                          "assessment": {"checkpoint_revision": LAYA_REVISION}})
        handler = partial(OpenBotHandler, store=store, investigator=None, token="demo-bearer",
                          bank_url=self.origin + "/bank", specialists=None)
        server = self.start_server(handler)
        payload = {"threadId": "demo-thread", "runId": "demo-run",
                   "messages": [{"role": "user", "content": "Watch " + case_id}]}
        headers = {"Authorization": "Bearer demo-bearer"}
        status, stream, _ = self.request("/ag-ui", payload, headers=headers, server=server)
        self.assertEqual(status, 200)
        self.assertIn("Previous decision agent", stream)
        self.assertIn("Investigation Coordinator", stream)
        self.assertIn("Advisory", stream)
        self.assertIn("Laya", stream)
        self.assertIn(LAYA_MODEL, stream)
        self.assertIn(LAYA_REVISION[:12], stream)
        self.assertIn("Payment status: Reversed", stream)
        self.assertIn("RUN_FINISHED", stream)
        status, stream, _ = self.request("/ag-ui/coordinator", payload, headers=headers, server=server)
        self.assertEqual(status, 200)
        self.assertIn("nvidia/coordinator-model", stream)
        self.assertNotIn("nvidia/old-hosted-model", stream)
        self.assertNotIn(LAYA_MODEL, stream)
