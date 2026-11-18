"""On-demand bank review agents for one simulated payment."""

from dataclasses import dataclass
import json
import os
import re
from threading import BoundedSemaphore

from hosted_agent import HostedModel
from investigation import GUIDANCE


@dataclass(frozen=True)
class Specialist:
    name: str
    model: str
    task: str
    fields: tuple[str, ...]


LIGHTNING = "nvidia/nemotron-3.5-lightning-30b-a3b"
SPECIALISTS = {
    "signal-analyst": Specialist(
        "Signal Analyst", "nvidia/nemotron-3-ultra-550b-a55b",
        "Compare the payment with sender and recipient history. Explain the strongest signal and its limits.",
        ("reasons", "risk", "sender_history", "recipient_activity"),
    ),
    "case-brief": Specialist(
        "Case Brief Writer", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        "Write a concise handoff for the next reviewer with the status, documented flags, and evidence still needed.",
        ("reasons", "investigation_status", "investigation_tools"),
    ),
    "ledger-reviewer": Specialist(
        "Ledger Reviewer", "nvidia/nemotron-3.5-bolt-e4b",
        "Explain the entries in order, including the effect on reserved funds and whether a receiver credit posted. "
        "Use the provided net total. Report any discrepancy without suggesting a ledger edit.",
        ("journal", "net_postings_paise"),
    ),
    "verification-coach": Specialist(
        "Verification Coach", "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "Create a short reviewer checklist using the supplied demo procedure. Mark which checks still need human confirmation.",
        ("reasons", "verification_procedure"),
    ),
    "policy-explainer": Specialist(
        "Policy Explainer", LIGHTNING,
        "Explain the recorded policy version, why this payment was flagged when applicable, and how its score relates "
        "to the review threshold. Distinguish the score from independent policy flags.",
        ("reasons", "verification_reasons", "risk", "policy_version"),
    ),
}


def key_setting(agent):
    return f"UPI_AGENT_{agent.replace('-', '_').upper()}_KEY"


SPECIALIST_SETTINGS = {key_setting(agent) for agent in SPECIALISTS}


class SpecialistTeam:
    def __init__(self):
        self.models = {}
        for agent, spec in SPECIALISTS.items():
            key = os.getenv(key_setting(agent), "").strip()
            if key:
                self.models[agent] = HostedModel("https://integrate.api.nvidia.com/v1", spec.model, key)
        self.slots = {agent: BoundedSemaphore(1) for agent in self.models}

    def public(self):
        return [{"id": agent, "name": SPECIALISTS[agent].name, "model": provider.model}
                for agent, provider in self.models.items()]

    def answer(self, agent, case):
        spec = SPECIALISTS[agent]
        model = self.models.get(agent)
        if model is None:
            return f"{spec.name} is awaiting its hosted model key."
        if not self.slots[agent].acquire(blocking=False):
            return f"{spec.name} is reviewing another case. Try again shortly."
        try:
            facts = {"amount_paise": case["amount"], "status": case["status"]}
            evidence = {
                **case, "verification_procedure": GUIDANCE,
                "investigation_tools": (case["report"] or {}).get("trace", []),
            }
            facts.update({name: evidence[name] for name in spec.fields})
            instructions = (
                f"You are the {spec.name} for a simulated UPI bank review. {spec.task} "
                "Use only the provided case facts. Amounts are integer paise. State when evidence is missing. "
                "Only a bank reviewer can decide a payment. Reply in plain text, under 100 words, without links."
            )
            response = model.complete({
                "model": model.model, "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": json.dumps(facts)},
                ],
                "stream": False, "max_tokens": 450, "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=20)
            choice = response["choices"][0]
            answer = choice["message"].get("content")
            if (choice.get("finish_reason") == "length" or not isinstance(answer, str)
                    or not 1 <= len(answer.strip()) <= 1400):
                raise ValueError("Missing or incomplete specialist response.")
            escaped = re.sub(r"([\\`*{}\[\]<>()_~|])", r"\\\1", answer.strip())
            return f"**{spec.name}** · `{spec.model}`\n\n{escaped}"
        except (OSError, LookupError, TypeError, ValueError, AttributeError):
            return f"{spec.name} could not complete its review. The case evidence is available in Bank operations."
        finally:
            self.slots[agent].release()
