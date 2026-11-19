import json
import unittest
from unittest.mock import patch

from hosted_agent import HostedModel
from investigation import investigate
from openbot_bridge import latest_user_text


CASE_EVIDENCE = {
    "reasons": ["Amount is unusually high"],
    "sender_history": {"prior_payments": 4, "median_paise": 45000, "first_payment_to_recipient": False},
    "recipient_activity": {"window_hours": 24, "distinct_senders": 17},
    "risk": {"flagged": True},
}


def reply(name, arguments, token):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": token, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
    ]}}], "usage": {"prompt_tokens": 40, "completion_tokens": 12}}


class HostedInvestigationTest(unittest.TestCase):
    def setUp(self):
        self.model = HostedModel("https://provider.example/v1", "demo-tool-model", "example-test-key")

    def test_round_trip_preserves_tool_id_and_cites_results(self):
        output = [
            reply("recipient_activity", {}, "call1"),
            reply("sender_history", {}, "call2"),
            reply("verification_guidance", {}, "call3"),
            reply("submit_advisory", {
                "action": "request_verification", "summary": "Verify the payment against the procedure.",
                "evidence": ["recipient_activity", "sender_history", "verification_guidance"],
            }, "call4"),
        ]
        requests = []

        def completion(payload, timeout):
            requests.append(json.loads(json.dumps(payload)))
            self.assertGreater(timeout, 0)
            return output.pop(0)

        chooser = self.model.case()
        with patch.object(HostedModel, "complete", side_effect=completion):
            report = investigate(4500000, CASE_EVIDENCE, chooser)
        self.assertEqual(report["mode"], "agent")
        self.assertEqual(report["trace"], ["recipient_activity", "sender_history", "verification_guidance"])
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call1")
        self.assertEqual(json.loads(requests[1]["messages"][-1]["content"])["distinct_senders"], 17)
        self.assertEqual([tool["function"]["name"] for tool in requests[-1]["tools"]], ["submit_advisory"])
        self.assertEqual(chooser.usage, {"prompt_tokens": 160, "completion_tokens": 48})
        self.assertNotIn("example-test-key", repr(self.model))

    def test_untrusted_tool_or_invalid_advisory_goes_to_manual_review(self):
        invalid = [
            reply("release_payment", {}, "call1"),
            reply("sender_history", {"other_case": "1234"}, "call2"),
            reply("submit_advisory", {"action": "request_verification", "summary": "Approve",
                                    "evidence": ["verification_guidance"]}, "call3"),
            [],
        ]
        for response in invalid:
            with self.subTest(response=response):
                with patch.object(HostedModel, "complete", return_value=response):
                    report = investigate(4500000, CASE_EVIDENCE, self.model.case())
                self.assertEqual(report["mode"], "manual")

    def test_nvidia_catalog_uses_compatible_tool_request(self):
        provider = HostedModel("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3.5-lightning-30b-a3b", "test-key")
        advisory = {"action": "escalate", "summary": "Ask the bank to verify the amount.", "evidence": ["sender_history"]}
        with patch.object(HostedModel, "complete", return_value=reply("sender_history", {}, "call")) as complete:
            chooser = provider.case()
            result = chooser({"amount_paise": 4500000, "available_tools": {"sender_history": "Read history"}}, 3)
            complete.return_value = reply("submit_advisory", advisory, "final")
            final = chooser({"available_tools": {}, "results": {"sender_history": {"prior_payments": 4}}}, 3)
            initial_payload, timeout = complete.call_args_list[0].args
            final_payload, _ = complete.call_args.args
        self.assertEqual(result, {"tool": "sender_history"})
        self.assertEqual(timeout, 3)
        self.assertEqual(initial_payload["tool_choice"], "auto")
        self.assertGreater(initial_payload["max_tokens"], 0)
        self.assertFalse(initial_payload["stream"])
        self.assertEqual(initial_payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("max_completion_tokens", initial_payload)
        self.assertEqual(final_payload["tool_choice"], {"type": "function", "function": {"name": "submit_advisory"}})
        self.assertEqual(final, advisory)

    def test_openbot_uses_latest_user_text(self):
        messages = [
            {"role": "system", "content": "A server message"},
            {"role": "user", "content": "first"},
            {"role": "user", "content": [{"type": "text", "text": "Review"}, {"type": "text", "text": "case"}]},
            {"role": "assistant", "content": "An assistant message"},
        ]
        self.assertEqual(latest_user_text(messages), "Review case")
