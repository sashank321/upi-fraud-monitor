"""Hosted investigation using the Chat Completions tool-calling protocol."""
from dataclasses import dataclass, field
import json
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SYSTEM_PROMPT = """Investigate one simulated UPI payment. Use the supplied tools to corroborate the
risk signals before sending an advisory. Retrieve every required source and verification guidance
before recommending verification. Escalate when evidence is insufficient. Cite the tool names used
in the evidence array using the exact function names you called. Distinguish observations from
uncertainty. A bank reviewer resolves the payment. Call one function from the current tool list
at a time. Finish with submit_advisory."""
REPORT_TOOL = "submit_advisory"
MAX_OUTPUT_TOKENS = 1600
MAX_RESPONSE_BYTES = 1024 * 1024
NEMOTRON_MODELS_WITH_THINKING_CONTROL = {
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HostedModel:
    base_url: str
    model: str
    api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        key = os.getenv("UPI_LLM_API_KEY", "").strip()
        model = os.getenv("UPI_LLM_MODEL", "").strip()
        if not key or not model:
            return None
        url = os.getenv("UPI_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1").strip().rstrip("/")
        parsed = urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("UPI_LLM_BASE_URL must be an API base URL without credentials or a query.")
        if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
            raise ValueError("UPI_LLM_BASE_URL must use HTTPS, except for loopback testing.")
        return cls(url, model, key)

    def complete(self, payload, timeout):
        request = Request(f"{self.base_url}/chat/completions", json.dumps(payload).encode("utf-8"),
                          {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("Hosted model response exceeded its size limit.")
        return json.loads(data)

    def case(self):
        return HostedCase(self)


def function_tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False},
    }}


class HostedCase:
    def __init__(self, provider):
        self.provider = provider
        self.messages = []
        self.pending = None
        self.usage = {}

    def __call__(self, context, timeout):
        if not self.messages:
            case = {key: value for key, value in context.items() if key not in {"results", "available_tools"}}
            self.messages = [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": json.dumps(case)}]
        if self.pending:
            name, call_id = self.pending
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps(context["results"][name])})
            self.pending = None

        available = context["available_tools"]
        definitions = [function_tool(name, description, {}, []) for name, description in available.items()]
        report_fields = {
            "action": {"type": "string", "enum": ["request_verification", "escalate"]},
            "summary": {"type": "string", "maxLength": 600},
            "evidence": {"type": "array", "items": {"type": "string"},
                         "description": "Exact function names for evidence already retrieved."},
        }
        definitions.append(function_tool(REPORT_TOOL, "Return the final case advisory.",
                                         report_fields, list(report_fields)))
        payload = {
            "model": self.provider.model, "messages": self.messages, "tools": definitions,
            "stream": False, "parallel_tool_calls": False,
        }
        if urlsplit(self.provider.base_url).hostname == "integrate.api.nvidia.com":
            payload.update(tool_choice="auto", max_tokens=MAX_OUTPUT_TOKENS)
            if self.provider.model in NEMOTRON_MODELS_WITH_THINKING_CONTROL:
                payload.update(temperature=1.0, top_p=0.95, chat_template_kwargs={"enable_thinking": False})
                if not available:
                    payload["tool_choice"] = {"type": "function", "function": {"name": REPORT_TOOL}}
        else:
            payload.update(tool_choice="required", max_completion_tokens=MAX_OUTPUT_TOKENS)
        envelope = self.provider.complete(payload, timeout)
        if not isinstance(envelope, dict):
            raise ValueError("Invalid hosted model response.")
        usage = envelope.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        for name in ("prompt_tokens", "completion_tokens"):
            if type(usage.get(name)) is int and usage[name] >= 0:
                self.usage[name] = self.usage.get(name, 0) + usage[name]
        message = envelope["choices"][0]["message"]
        if not isinstance(message, dict):
            raise ValueError("Invalid hosted assistant message.")
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise ValueError("Expected exactly one tool call.")
        call = calls[0]
        if not isinstance(call, dict):
            raise ValueError("Invalid function call.")
        call_id = call["id"]
        if call.get("type") != "function" or not isinstance(call_id, str) or not 1 <= len(call_id) <= 200:
            raise ValueError("Invalid function call.")
        name = call["function"]["name"]
        arguments = json.loads(call["function"]["arguments"])
        if name == REPORT_TOOL and isinstance(arguments, dict):
            return arguments
        if not isinstance(name, str) or name not in available or arguments != {}:
            raise ValueError("Invalid evidence request.")
        assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
        if "reasoning_details" in message:  # Some routed reasoning models require this round trip.
            assistant["reasoning_details"] = message["reasoning_details"]
        self.messages.append(assistant)
        self.pending = name, call_id
        return {"tool": name}
