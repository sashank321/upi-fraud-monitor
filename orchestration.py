"""Local Laya decisions and an evidence-sharing panel for the simulated ledger."""

from dataclasses import replace
import json
import logging
import time
from urllib.error import HTTPError

from hosted_agent import HostedModel, function_tool
from specialists import LIGHTNING, SPECIALISTS


LAYA = "laya"
COORDINATOR = "coordinator"
REVIEW_ORDER = (COORDINATOR, "signal-analyst", "ledger-reviewer", "verification-coach", "case-brief", "policy-explainer")
REVIEW_ASSIGNMENTS = {
    COORDINATOR: "Coordinate the investigation. Analyze the original payment and Laya's screening result, explain the numerical anomaly or missing evidence, and identify what the specialists need to investigate.",
    "signal-analyst": "Compare the amount and recipient behaviour to the historical aggregates. Identify plausible benign explanations and material warning signs.",
    "ledger-reviewer": "Check the recorded ledger reservation, sum and account buckets. Distinguish ledger consistency from fraud evidence. Respond to the signal analysis.",
    "verification-coach": "Identify what the available evidence can verify. Respond to the previous reviews. Do not claim a person confirmed the payment.",
    "case-brief": "Summarize the evidence and disagreements for the next reviewer. Respond to the previous agents and make an independent recommendation.",
    "policy-explainer": "Act as a skeptical independent reviewer. Challenge unsupported conclusions in the earlier notes and identify uncertainty relevant to the final decision.",
}
TOOL = "submit_payment_assessment"
VALID_STAGE = {"screen": ("clear", "hold"), "review": ("settle", "reverse", "uncertain"),
               "final": ("settle", "reverse")}


class PaymentOrchestrator:
    def __init__(self, store, model, specialists, decision_engine=None):
        self.store = store
        self.providers = {**specialists.models, COORDINATOR: model}
        self.decision_engine = decision_engine

    @staticmethod
    def agent_name(agent):
        return {LAYA: "Laya", COORDINATOR: "Investigation Coordinator"}.get(agent) or SPECIALISTS[agent].name

    def agents(self):
        return [{"id": LAYA, "name": "Laya", "model": self.decision_engine.model if self.decision_engine else None,
                 "role": "Local typed decision engine: screening and final settlement or reversal",
                 "source": "https://github.com/NandhaKishorM/laya"}] + [
            {"id": agent, "name": self.agent_name(agent),
             "model": provider.model if provider else None,
             "fallback_model": LIGHTNING if provider and provider.model != LIGHTNING else None,
             "role": REVIEW_ASSIGNMENTS[agent]}
            for agent in REVIEW_ORDER for provider in [self.providers.get(agent)]
        ]

    def run_next(self):
        job = self.store.claim_run()
        if job:
            self.run(*job)

    def run(self, transfer_id, token):
        deadline = time.monotonic() + 120
        case = self.store.dialogue(transfer_id)
        if not case:
            return
        turns = case["turns"]
        if case["phase"] == "screening":
            turn = self.assess(LAYA, "screen", case, turns, None, deadline)
            if not self.store.publish_turn(transfer_id, token, turn) or turn["decision"] != "hold":
                return
            turns.append(turn)
        completed = {turn["agent"] for turn in turns if turn["stage"] == "review"}
        for agent in REVIEW_ORDER:
            if agent in completed:
                continue
            turn = self.assess(agent, "review", case, turns, REVIEW_ASSIGNMENTS[agent], deadline - 14)
            if not self.store.publish_turn(transfer_id, token, turn):
                return
            turns.append(turn)
        end = self.assess(LAYA, "final", case, turns, None, deadline)
        self.store.publish_turn(transfer_id, token, end)

    @staticmethod
    def system_turn(stage, decision, summary):
        return {"agent": "system", "stage": stage, "model": None,
                "audience": "Ledger", "decision": decision, "summary": summary}

    def assess(self, agent, stage, case, turns, task, deadline):
        if agent == LAYA:
            try:
                remaining = deadline - time.monotonic()
                if not self.decision_engine or remaining <= 0:
                    raise ValueError("Laya unavailable")
                turn = self.decision_engine.decide(stage, case, turns, min(remaining, 12))
                if stage == "final":
                    reviews = {entry["agent"]: entry["decision"] for entry in turns if entry["stage"] == "review"}
                    if any(reviews.get(agent) in {None, "unavailable"} for agent in REVIEW_ORDER):
                        turn["decision"] = "reverse"
                        turn["summary"] += " Incomplete investigation safeguard: return the reservation."
                    elif "settle" not in reviews.values() and turn["assessment"]["raw_choice"] == "settle":
                        turn["decision"] = "reverse"
                        turn["summary"] += " No reviewer supported release; the reservation was returned."
                turn["audience"] = self.audience(agent, stage, turn["decision"])
                return turn
            except (OSError, LookupError, AttributeError, TypeError, ValueError) as error:
                logging.warning("Local Laya decision unavailable (%s)", type(error).__name__)
                return self.system_turn(stage, "reverse", "The local Laya engine could not validate a decision. The reservation was returned to the sender.")
        provider = self.providers.get(agent)
        attempts = [provider] if provider else []
        if provider and stage == "review" and provider.model != LIGHTNING:
            attempts.append(replace(provider, model=LIGHTNING))
        for attempt in attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return self.request_assessment(attempt, agent, stage, case, turns, task, min(remaining, 12))
            except (OSError, LookupError, AttributeError, TypeError, ValueError) as error:
                logging.warning("Assessment unavailable for %s on %s (%s)", agent, attempt.model, type(error).__name__)
                if isinstance(error, HTTPError) and error.code in {401, 403, 429}:
                    break
        if stage != "review":
            return self.system_turn(stage, "reverse", "Laya could not validate a decision. The reservation was returned to the sender.")
        return {"agent": agent, "stage": stage, "model": provider.model if provider else None,
                "audience": "Review panel", "decision": "unavailable", "summary": "A valid assessment was unavailable."}

    @staticmethod
    def request_assessment(provider: HostedModel, agent, stage, case, turns, task, timeout):
        decisions = VALID_STAGE[stage]
        typical = case["sender_history"]["median_paise"]
        facts = {
            "amount_rupees": case["amount"] / 100,
            "typical_amount_rupees": typical / 100 if typical else None,
            "times_typical_amount": round(case["amount"] / typical, 2) if typical else None,
            "recipient_familiar": not case["sender_history"]["first_payment_to_recipient"],
            "prior_sender_payments": case["sender_history"]["prior_payments"],
            "recipient_activity": case["recipient_activity"],
        }
        if stage != "screen":
            facts["ledger"] = case["journal"]
            facts["net_postings_paise"] = case["net_postings_paise"]
            facts["discussion"] = [{field: turn[field] for field in ("agent", "decision", "summary")} for turn in turns]
        fields = {"decision": {"type": "string", "enum": list(decisions)},
                  "summary": {"type": "string", "maxLength": 440}}
        payload = {
            "model": provider.model, "stream": False, "max_tokens": 600,
            "messages": [
                {"role": "system", "content": (
                    f"You are {agent} in a simulated UPI review. {task} "
                    "Use only the supplied observations. Fields ending in _rupees are in rupees; fields ending in _paise are in paise. "
                    "Make the first sentence justify the decision you submit. If there is a discussion, address a previous finding. "
                    "A review recommendation of 'settle' means release the payment, 'reverse' means refund, and 'uncertain' means more evidence is needed. "
                    "Return one brief, evidence-backed message via the named tool. Never output URLs or Markdown."
                )},
                {"role": "user", "content": json.dumps(facts)},
            ],
            "tools": [function_tool(TOOL, "Submit your evidence-backed assessment.", fields, list(fields))],
            "tool_choice": {"type": "function", "function": {"name": TOOL}},
            "parallel_tool_calls": False,
        }
        if provider.model.startswith("nvidia/nemotron"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload.update(temperature=0.2, top_p=0.95)
        response = provider.complete(payload, timeout)
        [call] = response["choices"][0]["message"]["tool_calls"]
        if call["type"] != "function" or call["function"]["name"] != TOOL:
            raise ValueError("Invalid assessment tool call.")
        result = json.loads(call["function"]["arguments"])
        if (not isinstance(result, dict) or set(result) != set(fields) or result["decision"] not in decisions
                or not isinstance(result["summary"], str) or not result["summary"].strip()):
            raise ValueError("Invalid payment assessment.")
        summary = result["summary"].strip()
        if len(summary) > 440:
            summary = summary[:437].rstrip() + "…"
        return {"agent": agent, "stage": stage, "model": provider.model, "decision": result["decision"],
                "summary": summary,
                "audience": PaymentOrchestrator.audience(agent, stage, result["decision"])}

    @staticmethod
    def audience(agent, stage, decision):
        if stage == "final" or (stage == "screen" and decision != "hold"):
            return "Ledger"
        next_agent = REVIEW_ORDER[0] if stage == "screen" else (
            REVIEW_ORDER[REVIEW_ORDER.index(agent) + 1] if agent != REVIEW_ORDER[-1] else LAYA
        )
        return PaymentOrchestrator.agent_name(next_agent)
