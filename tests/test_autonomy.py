import json
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autonomous_store import AutonomousStore
from demo_auth import DemoAuth
from laya_decisions import LayaDecisionEngine, MODEL, REVISION
from orchestration import COORDINATOR, PaymentOrchestrator, REVIEW_ORDER, TOOL
from payments import PaymentError
from specialists import LIGHTNING


class Provider:
    def __init__(self, agent, reply):
        self.model = LIGHTNING
        self.agent = agent
        self.reply = reply
        self.inputs = []

    def complete(self, payload, timeout):
        self.inputs.append(json.loads(payload["messages"][-1]["content"]))
        choices = payload["tools"][0]["function"]["parameters"]["properties"]["decision"]["enum"]
        decision = self.reply(choices)
        return {"choices": [{"message": {"tool_calls": [{"type": "function", "function": {
            "name": TOOL, "arguments": json.dumps({"decision": decision, "summary": self.agent + " assessment."}),
        }}]}}]}


def checkpoint_response(stage, choice, probability=0.98):
    options = ("clear", "hold") if stage == "screen" else ("settle", "reverse")
    return {"model": MODEL, "revision": REVISION, "answer": {
        "choice": choice, "probabilities": {
            option: probability if option == choice else 1 - probability for option in options
        },
    }, "usage": {"input_tokens": 36, "output_tokens": 1}}


class LayaTransport:
    def __init__(self, final, screen):
        self.inputs = []
        self.results = {"screen": checkpoint_response("screen", screen),
                        "final": checkpoint_response("final", final)}

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.inputs.append((request.full_url, body))
        result = self.results[body["stage"]]
        if isinstance(result, Exception):
            raise result
        return BytesIO(json.dumps(result).encode())


class AutonomyTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = AutonomousStore(Path(directory.name) / "payments.sqlite3")

    def orchestrator(self, final, screen="hold"):
        self.laya = LayaTransport(final, screen)
        transport = patch("laya_decisions.build_opener", return_value=self.laya)
        transport.start()
        self.addCleanup(transport.stop)
        coordinator = Provider(COORDINATOR, lambda choices: "settle")
        others = {agent: Provider(agent, lambda choices: "settle") for agent in REVIEW_ORDER if agent != COORDINATOR}
        return PaymentOrchestrator(self.store, coordinator, SimpleNamespace(models=others),
                                   decision_engine=LayaDecisionEngine())

    def test_panel_responds_in_order_and_settles_only_once(self):
        transfer_id, _ = self.store.transfer("canteen", 1000000, "request")
        self.assertEqual(self.store.investigation_case(transfer_id)["status"], "screening")
        self.assertEqual(self.store.transfer("canteen", 1000000, "request"), (transfer_id, False))
        with self.assertRaises(PaymentError):
            self.store.resolve(transfer_id, "release", "bypass", True)
        orchestrator = self.orchestrator("settle")
        orchestrator.run_next()
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "settled")
        self.assertTrue(case["considered_hold"])
        self.assertEqual([turn["agent"] for turn in case["turns"]], ["laya", *REVIEW_ORDER, "laya"])
        self.assertEqual([turn["model"] for turn in case["turns"]], [MODEL, *([LIGHTNING] * len(REVIEW_ORDER)), MODEL])
        self.assertEqual(case["turns"][-1]["assessment"]["checkpoint_revision"], REVISION)
        self.assertEqual(case["turns"][-1]["assessment"]["probabilities"]["settle"], 0.98)
        self.assertEqual([body["stage"] for _, body in self.laya.inputs], ["screen", "final"])
        self.assertTrue(all(url.endswith("/v1/decision") for url, _ in self.laya.inputs))
        decision_evidence = self.laya.inputs[-1][1]["state"]
        for index, name in enumerate(REVIEW_ORDER):
            discussion = orchestrator.providers[name].inputs[0]["discussion"]
            self.assertEqual([turn["agent"] for turn in discussion], ["laya", *REVIEW_ORDER[:index]])
            self.assertIn(name + " assessment.", decision_evidence)
        self.assertEqual(self.store.view("receiver", "canteen")["account"]["available"], 1000000)
        orchestrator.run_next()
        self.assertEqual(len(self.store.dialogue(transfer_id)["turns"]), len(REVIEW_ORDER) + 2)
        self.assertEqual(self.store.view("sender")["account"]["reserved"], 0)

    def test_clear_uses_laya_without_calling_hosted_reviewers(self):
        transfer_id, _ = self.store.transfer("canteen", 45000, "request")
        orchestrator = self.orchestrator("reverse", screen="clear")
        orchestrator.run_next()
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "settled")
        self.assertFalse(case["considered_hold"])
        self.assertEqual([(turn["agent"], turn["decision"]) for turn in case["turns"]], [("laya", "clear")])
        self.assertTrue(all(not provider.inputs for provider in orchestrator.providers.values()))
        self.assertEqual([body["stage"] for _, body in self.laya.inputs], ["screen"])

    def test_missing_specialist_reverses_and_restores_reservation(self):
        transfer_id, _ = self.store.transfer("rent", 2000000, "request")
        orchestrator = self.orchestrator("settle")
        del orchestrator.providers["signal-analyst"]
        orchestrator.run_next()
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "cancelled")
        self.assertEqual(case["turns"][-1]["agent"], "laya")
        self.assertEqual(case["turns"][-1]["assessment"]["raw_choice"], "settle")
        self.assertEqual(case["turns"][-1]["decision"], "reverse")
        self.assertEqual(self.store.view("sender")["account"]["available"], 10000000)
        self.assertEqual(self.store.view("receiver", "rent")["account"]["available"], 0)

    def test_uncertain_clear_and_settle_do_not_release_funds(self):
        transfer_id, _ = self.store.transfer("canteen", 45000, "request")
        orchestrator = self.orchestrator("settle", screen="clear")
        self.laya.results["screen"] = checkpoint_response("screen", "clear", probability=0.51)
        self.laya.results["final"] = checkpoint_response("final", "settle", probability=0.51)
        orchestrator.run_next()
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "cancelled")
        self.assertEqual((case["turns"][0]["assessment"]["raw_choice"], case["turns"][0]["decision"]),
                         ("clear", "hold"))
        self.assertEqual((case["turns"][-1]["assessment"]["raw_choice"], case["turns"][-1]["decision"]),
                         ("settle", "reverse"))
        self.assertEqual(self.store.view("sender")["account"]["available"], 10000000)

    def test_release_requires_support_from_the_completed_panel(self):
        transfer_id, _ = self.store.transfer("canteen", 4500000, "request")
        orchestrator = self.orchestrator("settle")
        for provider in orchestrator.providers.values():
            provider.reply = lambda choices: "uncertain"
        orchestrator.run_next()
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "cancelled")
        final = case["turns"][-1]
        self.assertEqual(final["assessment"]["raw_choice"], "settle")
        self.assertEqual(final["decision"], "reverse")
        self.assertIn("No reviewer supported release", final["summary"])
        self.assertEqual(self.store.view("receiver", "canteen")["account"]["available"], 0)

    def test_unavailable_or_invalid_laya_decision_cannot_release_funds(self):
        orchestrator = self.orchestrator("settle", screen="clear")
        for index, failure in enumerate((
            TimeoutError("Laya service did not answer"),
            dict(checkpoint_response("screen", "clear"), revision="wrong"),
            dict(checkpoint_response("screen", "clear"), usage={"truncated": True}),
        )):
            with self.subTest(failure=failure):
                transfer_id, _ = self.store.transfer("canteen", 45000, f"request-{index}")
                self.laya.results["screen"] = failure
                orchestrator.run_next()
                case = self.store.dialogue(transfer_id)
                self.assertEqual(case["status"], "cancelled")
                self.assertEqual([(turn["agent"], turn["model"]) for turn in case["turns"]], [("system", None)])
                self.assertEqual(self.store.view("sender")["account"]["available"], 10000000)
                self.assertEqual(self.store.view("sender")["account"]["reserved"], 0)
                self.assertEqual(self.store.view("receiver", "canteen")["account"]["available"], 0)

    def test_expiry_blocks_late_writes(self):
        transfer_id, _ = self.store.transfer("canteen", 1000000, "request")
        _, token = self.store.claim_run()
        with self.store.connection() as db:
            db.execute("UPDATE transfers SET expires_at=1 WHERE id=?", (transfer_id,))
        self.store.expire()
        late = self.orchestrator("settle").system_turn("screen", "clear", "Late decision")
        self.assertFalse(self.store.publish_turn(transfer_id, token, late))
        self.assertEqual(self.store.dialogue(transfer_id)["status"], "expired")
        self.assertEqual(self.store.view("sender")["account"]["reserved"], 0)

    def test_lease_reclaim_replays_recorded_turns_without_double_settlement(self):
        transfer_id, _ = self.store.transfer("canteen", 1000000, "request")
        _, abandoned = self.store.claim_run()
        orchestrator = self.orchestrator("settle")
        turn = {"stage": "screen", "agent": "laya", "model": MODEL, "audience": "Investigation Coordinator",
                "decision": "hold", "summary": "The payment needs review."}
        self.assertTrue(self.store.publish_turn(transfer_id, abandoned, turn))
        with self.store.connection() as db:
            db.execute("UPDATE payment_runs SET leased_until = 0")
        _, token = self.store.claim_run()
        self.assertNotEqual(abandoned, token)
        self.assertFalse(self.store.publish_turn(transfer_id, abandoned, turn))
        orchestrator.run(transfer_id, token)
        case = self.store.dialogue(transfer_id)
        self.assertEqual(case["status"], "settled")
        self.assertEqual(len(case["turns"]), len(REVIEW_ORDER) + 2)
        self.assertEqual([body["stage"] for _, body in self.laya.inputs], ["final"])

    def test_operator_switch_persists_until_logout_without_elevating_sender(self):
        signins = self.store.path.with_suffix(".signins.json")
        auth = DemoAuth(self.store, signins)
        passwords = json.loads(signins.read_text())
        bank, _ = auth.sign_in("bank", passwords["bank"], None, remember=True)
        header = auth.cookie(bank, remember=True)
        self.assertIn("Max-Age=31536000", header)
        self.assertEqual(auth.switch(header, "canteen")["account"], "canteen")
        self.assertEqual(DemoAuth(self.store, signins).session(header)["username"], "canteen")
        self.assertEqual(auth.switch(header, "bank")["role"], "bank")
        auth.sign_out(header)
        self.assertIsNone(auth.session(header))
        sender, _ = auth.sign_in("sender", passwords["sender"], None)
        with self.assertRaises(PaymentError):
            auth.switch(auth.cookie(sender), "bank")
