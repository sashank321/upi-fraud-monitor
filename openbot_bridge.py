"""Read-only AG-UI coworker endpoint for CopilotKit OpenBot."""
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler
import json
import re
import sqlite3
import time
from uuid import uuid4

from investigation import TOOLS
from laya_decisions import MODEL as LAYA_MODEL
from orchestration import COORDINATOR, PaymentOrchestrator
from specialists import SPECIALISTS


CASE_ID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", re.IGNORECASE)
MAX_REQUEST = 512 * 1024
PROBE = "This is an automated connection test from OpenBot. Reply with one short word."
STATUS = {"settled": "Settled", "held": "Held for agent review", "screening": "Screening",
          "cancelled": "Reversed", "expired": "Expired and refunded"}


def previous_decision(turn):
    return turn["agent"] == "laya" and turn["model"] != LAYA_MODEL


def latest_user_text(messages):
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(part["text"] for part in content
                                if isinstance(part, dict) and isinstance(part.get("text"), str))
            return ""
    return ""


def case_summary(case):
    amount = case["amount"] / 100
    status = "Held for bank review" if case["status"] == "held" and not case.get("phase") else STATUS[case["status"]]
    lines = [f"**{case['recipient']} · ₹{amount:,.2f}**", f"Payment status: {status}.",
             f"Case ID: `{case['id']}`"]
    if case["reasons"]:
        lines.extend(["", "**Review flags**", *(f"- {reason}" for reason in case["reasons"])])
    report = case["report"]
    if report:
        lines.extend(["", "**Investigation advisory**" if report["mode"] == "agent" else "**Manual review**",
                      report["summary"]])
        if report["trace"]:
            lines.extend(["", "Evidence reviewed: " + ", ".join(tool.replace("_", " ") for tool in report["trace"]) + "."])
    elif case["status"] == "held":
        lines.extend(["", "Investigation is pending. Check the bank dashboard for the live case."])
    return "\n".join(lines)


class OpenBotHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, investigator, token, bank_url, specialists=None, **kwargs):
        self.store = store
        self.investigator = investigator
        self.token = token
        self.bank_url = bank_url
        self.specialists = specialists
        super().__init__(*args, **kwargs)

    def do_POST(self):
        specialist = self.path.removeprefix("/ag-ui/")
        autonomous = getattr(self.store, "autonomous", False)
        supported = specialist in SPECIALISTS or (autonomous and specialist == COORDINATOR)
        if self.path != "/ag-ui" and (not self.path.startswith("/ag-ui/") or not supported):
            self.send_error(404)
            return
        presented = self.headers.get("Authorization", "").encode()
        if self.token and not compare_digest(presented, f"Bearer {self.token}".encode()):
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get_content_type() != "application/json" or not 1 <= length <= MAX_REQUEST:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            thread_id, run_id = body["threadId"], body["runId"]
            messages = body["messages"]
            if not all(isinstance(value, str) and 1 <= len(value) <= 200 for value in (thread_id, run_id)):
                raise ValueError
            if not isinstance(messages, list):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.send_error(400, "Expected an AG-UI RunAgentInput")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        identifiers = {"threadId": thread_id, "runId": run_id}
        try:
            self.event("RUN_STARTED", **identifiers)
            text = latest_user_text(messages)
            actor = specialist if self.path != "/ag-ui" else None
            message_id = uuid4().hex
            self.event("TEXT_MESSAGE_START", messageId=message_id, role="assistant")
            if autonomous:
                for part in self.conversation(text, actor):
                    self.event("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=part)
            else:
                self.event("TEXT_MESSAGE_CONTENT", messageId=message_id, delta=self.answer(text, actor))
            self.event("TEXT_MESSAGE_END", messageId=message_id)
            self.event("RUN_FINISHED", **identifiers)
        except (BrokenPipeError, ConnectionResetError):
            return
        except sqlite3.Error:
            self.event("RUN_ERROR", message="The case store is temporarily unavailable.")

    def event(self, event_type, **data):
        payload = json.dumps({"type": event_type, **data})
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def conversation(self, text, actor):
        if text.strip() == PROBE:
            yield "Ready."
            return
        matches = {value.lower() for value in CASE_ID.findall(text)}
        if len(matches) != 1:
            yield "Copy the OpenBot prompt from Bank operations or send `Watch <case ID>`."
            return
        transfer_id = matches.pop()
        case = self.store.dialogue(transfer_id)
        if case is None:
            yield "No simulated payment matches that case ID."
            return
        yield f"**{case['recipient']} · ₹{case['amount'] / 100:,.2f}**\n\n"
        if not case["phase"]:
            yield case_summary(case) + f"\n\n[Open case in Bank operations]({self.bank_url}?case={transfer_id})"
            return
        deadline = time.monotonic() + 125
        seen = set()
        while case:
            previous_final = any(turn["stage"] == "final" and previous_decision(turn) for turn in case["turns"])
            for turn in case["turns"]:
                if turn["sequence"] in seen or (actor and turn["agent"] != actor):
                    continue
                seen.add(turn["sequence"])
                if previous_decision(turn):
                    sender = "Previous decision agent"
                elif turn["agent"] == "system":
                    sender = "Ledger protection"
                elif turn["agent"] in {"laya", COORDINATOR, *SPECIALISTS}:
                    sender = PaymentOrchestrator.agent_name(turn["agent"])
                else:
                    sender = "Recorded agent"
                audience = "Previous decision agent" if previous_final and turn["audience"] == "Laya" else turn["audience"]
                model = f" · `{turn['model']}`" if turn["model"] else ""
                revision = turn.get("assessment", {}).get("checkpoint_revision")
                if revision:
                    model += f" · checkpoint `{revision[:12]}`"
                decision = ("Advisory · " if turn["stage"] == "review" else "") + turn["decision"].capitalize()
                summary = re.sub(r"([\\`*{}\[\]<>()_~|])", r"\\\1", turn["summary"])
                yield f"**{sender} → {audience}**{model}\n\n**{decision}** · {summary}\n\n"
            if case["status"] not in {"screening", "held"} or time.monotonic() >= deadline or "watch" not in text.lower():
                break
            time.sleep(0.5)
            case = self.store.dialogue(transfer_id)
        if case:
            if actor and not seen:
                yield "This agent has no recorded reply for this case.\n\n"
            yield f"**Payment status: {STATUS[case['status']]}.**\n\n[Open case in Bank operations]({self.bank_url}?case={transfer_id})"

    def answer(self, text, specialist=None):
        if text.strip() == PROBE:
            return "Ready."
        matches = {value.lower() for value in CASE_ID.findall(text)}
        if len(matches) != 1:
            return "Copy a case ID from Bank operations and send `Review <case ID>`."
        transfer_id = matches.pop()
        case = self.store.investigation_case(transfer_id, include_journal=specialist == "ledger-reviewer")
        if case is None:
            return "No simulated payment matches that case ID."
        if not specialist and self.investigator and case["status"] == "held" and case["report"] is None:
            job = self.store.claim(transfer_id)
            if job:
                self.investigator(job, self.step)
                case = self.store.investigation_case(transfer_id)
        label = "Open bank decision panel" if case["status"] == "held" else "Open live case in Bank operations"
        if specialist and self.specialists:
            answer = self.specialists.answer(specialist, case)
            latest = self.store.investigation_case(transfer_id)
            if latest and latest["status"] != case["status"]:
                answer += f"\n\nThe status changed during this review. Current status is **{STATUS[latest['status']]}**."
                label = "Open live case in Bank operations"
        elif specialist:
            answer = "This specialist is awaiting its hosted model configuration."
        else:
            answer = case_summary(case)
        return answer + f"\n\n[{label}]({self.bank_url}?case={transfer_id})"

    def step(self, tool):
        if tool in TOOLS:
            self.event("STEP_STARTED", stepName=tool)
            self.event("STEP_FINISHED", stepName=tool)
