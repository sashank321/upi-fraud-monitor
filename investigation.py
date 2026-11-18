import time


TOOLS = {
    "sender_history": "Inspect the payer's typical amount and history with this recipient.",
    "recipient_activity": "Inspect the recipient's number of distinct recent senders.",
    "verification_guidance": "Read the demo procedure for verifying unusually large or unfamiliar payments.",
}
GUIDANCE = {
    "source": "Demo verification procedure v1",
    "text": "Verify the exact amount and recipient using an independent contact channel. Unusual activity "
            "is a risk signal. If evidence is missing or conflicting, escalate for manual verification.",
}
ACTIONS = {"request_verification", "escalate"}
MAX_TOOLS = 3
def fallback(evidence, explanation, trace=None):
    return {
        "mode": "manual",
        "action": "escalate",
        "summary": explanation,
        "trace": trace or [],
        "evidence": {"sender_history": evidence["sender_history"],
                     "recipient_activity": evidence["recipient_activity"],
                     "verification_guidance": GUIDANCE},
    }


def investigate(amount, evidence, choose, on_tool=None):
    if choose is None:
        return fallback(evidence, "Hosted investigation is awaiting configuration. Review the stored evidence.")
    available = {"sender_history": evidence["sender_history"],
                 "recipient_activity": evidence["recipient_activity"], "verification_guidance": GUIDANCE}
    results = {}
    trace = []
    required_tools = ("sender_history", "recipient_activity") if (evidence.get("risk") or {}).get("flagged") else ()
    deadline = time.monotonic() + 30
    try:
        for _ in range(MAX_TOOLS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Investigation deadline reached.")
            decision = choose({
                "amount_paise": amount, "risk_reasons": evidence["reasons"],
                "rule_signals": evidence.get("signals", []),
                "required_tools": required_tools,
                "available_tools": {key: TOOLS[key] for key in available if key not in results},
                "results": dict(results),
            }, remaining)
            if time.monotonic() >= deadline or not isinstance(decision, dict):
                raise ValueError("Invalid or late model response.")
            if set(decision) == {"tool"}:
                name = decision["tool"]
                if not isinstance(name, str) or name not in available or name in results:
                    raise ValueError("Tool is unavailable.")
                results[name] = available[name]
                trace.append(name)
                if on_tool is not None:
                    on_tool(name)
                continue
            if set(decision) != {"action", "summary", "evidence"}:
                raise ValueError("Invalid report fields.")
            action, summary, refs = decision["action"], decision["summary"], decision["evidence"]
            if (not isinstance(action, str) or action not in ACTIONS or
                    not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 600 or
                    not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in results for ref in refs)):
                raise ValueError("Invalid report contents.")
            if action == "request_verification" and (
                "verification_guidance" not in refs
                or not {"sender_history", "recipient_activity"}.intersection(refs)
                or not set(required_tools).issubset(refs)
            ):
                raise ValueError("Verification recommendation requires evidence and guidance.")
            return {
                "mode": "agent", "action": action, "summary": summary.strip(), "trace": trace,
                "evidence": {ref: results[ref] for ref in refs},
            }
    except (OSError, LookupError, TypeError, ValueError):
        return fallback(evidence, "The hosted model did not complete a valid report. Review the stored evidence.", trace)
    return fallback(evidence, "The investigation reached its tool budget. Review the stored evidence.", trace)
