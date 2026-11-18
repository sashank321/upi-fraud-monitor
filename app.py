import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import logging
import os
from pathlib import Path
import sqlite3
from threading import Event, Thread
from urllib.parse import parse_qs, urlencode, urlsplit

from autonomous_store import AutonomousStore
from demo_auth import ACCOUNTS, DemoAuth
from hosted_agent import HostedModel
from laya_decisions import LayaDecisionEngine
from openbot_bridge import CASE_ID, OpenBotHandler
from orchestration import PaymentOrchestrator
from payments import ORCHESTRATION_POLICY, POLICY_VERSION, PaymentError
from specialists import SPECIALIST_SETTINGS, SpecialistTeam


ROOT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui.css": ("ui.css", "text/css; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
}
VIEWS = {"/login", "/sender", "/receiver", "/bank", "/guide"}
PRIVATE_VIEWS = {"/sender", "/receiver", "/bank"}


def demo_url(value, port=None):
    if not value:
        return None
    url = urlsplit(value)
    if url.scheme != "http" or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        raise ValueError("Use an HTTP demo origin such as http://192.168.0.10:8000.")
    address = ip_address(url.hostname or "")
    if address.version != 4 or address.is_unspecified or not address.is_private or not url.port:
        raise ValueError("The demo origin requires a private IPv4 address and an explicit port.")
    if port is not None and url.port != port:
        raise ValueError("UPI_PUBLIC_URL must match the monitor port.")
    return f"http://{address}:{url.port}"


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, store, auth, model, public_url, openbot_url, specialists=None, orchestrator=None, **kwargs):
        self.store = store
        self.auth = auth
        self.model = model
        self.public_url = public_url
        self.openbot_url = openbot_url
        self.specialists = specialists
        self.orchestrator = orchestrator
        super().__init__(*args, **kwargs)

    def send_data(self, status, body, content_type, **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data, **headers):
        self.send_data(status, json.dumps(data).encode(), "application/json; charset=utf-8", **headers)

    def check_origin(self):
        port = self.server.server_port
        host = self.headers.get("Host")
        allowed = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if self.public_url:
            allowed.add(urlsplit(self.public_url).netloc)
        if host not in allowed:
            raise PaymentError("Use the demo URL printed by the server.", 403)
        origin = self.headers.get("Origin")
        if (origin is not None or self.command == "POST") and origin != f"http://{host}":
            raise PaymentError("Request origin does not match the demo.", 403)

    def identity(self, role=None):
        user = self.auth.session(self.headers.get("Cookie"))
        if user is None:
            raise PaymentError("Sign in to continue.", 401)
        if role and user["role"] != role:
            raise PaymentError("This interface requires a different demo account.", 403)
        return user

    def communication_models(self):
        return {
            "policy": ORCHESTRATION_POLICY if self.orchestrator else POLICY_VERSION,
            "investigator": self.model or None,
            "specialists": self.specialists.public() if self.openbot_url and self.specialists else [],
        }

    def do_GET(self):
        try:
            self.check_origin()
            url = urlsplit(self.path)
            if url.path in {"/api/state", "/api/bank"}:
                self.identity("bank")
                case_id = parse_qs(url.query).get("case", [""])[0]
                state = self.store.snapshot(case_id=case_id if CASE_ID.fullmatch(case_id) else None)
                state["model"] = self.model or None
                state["openbot"] = self.openbot_url
                state["communication"] = self.communication_models()
                state["specialists"] = state["communication"]["specialists"]
                state["agents"] = self.orchestrator.agents() if self.orchestrator else []
                self.send_json(200, state)
            elif url.path.startswith("/api/bank/case/"):
                self.identity("bank")
                case_id = url.path.removeprefix("/api/bank/case/")
                case = self.store.dialogue(case_id) if self.orchestrator and CASE_ID.fullmatch(case_id) else None
                if case is None:
                    raise PaymentError("Case not found.", 404)
                self.send_json(200, case)
            elif url.path == "/api/bank/feedback":
                self.identity("bank")
                self.send_json(200, self.store.review_feedback())
            elif url.path == "/api/sender":
                self.identity("sender")
                self.send_json(200, self.store.view("sender"))
            elif url.path == "/api/receiver":
                recipient = self.identity("receiver")["account"]
                self.send_json(200, self.store.view("receiver", recipient))
            elif url.path == "/api/session":
                self.send_json(200, {"user": self.auth.session(self.headers.get("Cookie")),
                                     "accounts": [self.auth.identity(username) for username in ACCOUNTS]})
            elif url.path == "/api/openbot-access":
                self.identity("bank")
                self.send_data(204, b"", "text/plain")
            elif url.path == "/api/guide":
                self.send_json(200, {"agent": self.model or None,
                                     "communication": self.communication_models(),
                                     "agents": self.orchestrator.agents() if self.orchestrator else []})
            elif url.path == "/health":
                self.send_json(200, {"service": "upi-fraud-monitor", "views": sorted(VIEWS - {"/"}),
                                     "investigation_model": self.model or None, "openbot": bool(self.openbot_url),
                                     "orchestration": bool(self.orchestrator)})
            elif url.path == "/favicon.ico":
                self.send_data(204, b"", "image/x-icon")
            elif url.path == "/" or url.path in PRIVATE_VIEWS:
                user = self.auth.session(self.headers.get("Cookie"))
                if user and url.path == "/" + user["role"]:
                    self.send_page()
                else:
                    destination = "/" + user["role"] if user else "/login"
                    if url.path in PRIVATE_VIEWS and (not user or user["role"] != url.path[1:]):
                        target = url.path
                        case_id = parse_qs(url.query).get("case", [""])[0]
                        if url.path == "/bank" and CASE_ID.fullmatch(case_id):
                            target += "?case=" + case_id
                        if not user:
                            destination += "?" + urlencode({"next": target})
                        else:
                            destination = "/login?" + urlencode({"next": target, "switch": 1})
                    self.send_data(303, b"", "text/plain", Location=destination)
            elif url.path in ASSETS or url.path in VIEWS:
                filename, content_type = ASSETS["/" if url.path in VIEWS else url.path]
                self.send_data(200, (ROOT / "web" / filename).read_bytes(), content_type)
            else:
                self.send_json(404, {"error": "Page not found."})
        except PaymentError as error:
            self.send_json(error.status, {"error": str(error)})
        except sqlite3.Error:
            logging.exception("State lookup failed")
            self.send_json(503, {"error": "The demo database is temporarily unavailable."})

    def send_page(self):
        filename, content_type = ASSETS["/"]
        self.send_data(200, (ROOT / "web" / filename).read_bytes(), content_type)

    def read_payload(self):
        if self.headers.get_content_type() != "application/json":
            raise PaymentError("Send application/json.", 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 4096:
                raise ValueError
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError
        except ValueError as error:
            raise PaymentError("Send a valid JSON object under 4 KB.") from error
        return data

    def do_POST(self):
        try:
            self.check_origin()
            data = self.read_payload()
            if self.path == "/api/login":
                remember = data.get("remember") is True
                token, user = self.auth.sign_in(data.get("username"), data.get("password"), self.headers.get("Cookie"),
                                                remember=remember)
                self.send_json(200, {"user": user}, Set_Cookie=self.auth.cookie(token, remember))
            elif self.path == "/api/session/switch":
                self.send_json(200, {"user": self.auth.switch(self.headers.get("Cookie"), data.get("username"))})
            elif self.path == "/api/logout":
                self.auth.sign_out(self.headers.get("Cookie"))
                self.send_json(200, {}, Set_Cookie=self.auth.cookie())
            elif self.path == "/api/transfers":
                self.identity("sender")
                transfer_id, created = self.store.transfer(
                    data.get("payee"), data.get("amount"), self.headers.get("Idempotency-Key"),
                )
                self.send_json(201 if created else 200, {"id": transfer_id})
            elif self.path == "/api/bank/reset-sender-balance":
                bank = self.identity("bank")
                self.send_json(200, self.store.reset_sender_balance(bank["username"], data.get("expected_available")))
            elif self.path.startswith("/api/reviews/"):
                self.identity("bank")
                transfer_id = self.path.removeprefix("/api/reviews/")
                self.store.resolve(transfer_id, data.get("action"), data.get("note"), data.get("verified"))
                self.send_json(200, {"id": transfer_id})
            else:
                self.send_json(404, {"error": "Endpoint not found."})
        except PaymentError as error:
            self.send_json(error.status, {"error": str(error)})
        except sqlite3.Error:
            logging.exception("Payment operation failed")
            self.send_json(503, {"error": "The demo database is temporarily unavailable."})


def work_until_stopped(stop, task, interval):
    while not stop.is_set():
        try:
            task()
        except Exception:
            logging.exception("Background task failed")
        stop.wait(interval)


AGENT_SETTINGS = {"UPI_LLM_BASE_URL", "UPI_LLM_MODEL", "UPI_LLM_API_KEY", "UPI_OPENBOT_TOKEN", "UPI_LAYA_URL"} | SPECIALIST_SETTINGS


def load_config(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator and name in AGENT_SETTINGS:
            if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            os.environ.setdefault(name, value)


def serve(database, port, bind, ag_ui_port, ag_ui_internal, public_url=None, signins=None):
    public_url = demo_url(public_url, port)
    openbot_url = demo_url(os.getenv("UPI_OPENBOT_PUBLIC_URL"))
    store = AutonomousStore(database)
    auth = DemoAuth(store, signins if signins else database.with_suffix(".signins.json"))
    model = HostedModel.from_environment()
    specialists = SpecialistTeam()
    orchestrator = PaymentOrchestrator(store, model, specialists, LayaDecisionEngine.from_environment())
    token = os.getenv("UPI_OPENBOT_TOKEN", "").strip()
    if ag_ui_port is not None and not ag_ui_internal and len(token) < 32:
        raise ValueError("Set a private UPI_OPENBOT_TOKEN with at least 32 characters for the local AG-UI endpoint.")

    stop = Event()
    handler = partial(Handler, store=store, auth=auth, model=model.model if model else None,
                      public_url=public_url, openbot_url=openbot_url, specialists=specialists,
                      orchestrator=orchestrator)
    with ThreadingHTTPServer((bind, port), handler) as server:
        bridge = None
        if ag_ui_port is not None:
            agent_handler = partial(OpenBotHandler, store=store, investigator=None,
                                    token="" if ag_ui_internal else token,
                                    bank_url=(public_url or f"http://127.0.0.1:{server.server_port}") + "/bank",
                                    specialists=specialists)
            bridge = ThreadingHTTPServer(("0.0.0.0" if ag_ui_internal else "127.0.0.1", ag_ui_port),
                                         agent_handler)
            Thread(target=bridge.serve_forever, daemon=True).start()
        Thread(target=work_until_stopped, args=(stop, store.expire, 1), daemon=True).start()
        for _ in range(2):
            Thread(target=work_until_stopped, args=(stop, orchestrator.run_next, 0.4), daemon=True).start()
        Thread(target=work_until_stopped, args=(stop, auth.purge, 3600), daemon=True).start()
        print(f"UPI Fraud Monitor is running at {public_url or f'http://127.0.0.1:{server.server_port}'}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if bridge:
                bridge.shutdown()
                bridge.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local UPI Fraud Monitor demo.")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "demo.sqlite3")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", type=Path, default=ROOT / "data" / "openbot.env")
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--public-url", help="Demo address reachable by your team on the LAN")
    parser.add_argument("--signins", type=Path, help="Private demo credential file")
    parser.add_argument("--ag-ui-port", type=int)
    parser.add_argument("--ag-ui-internal", action="store_true", help="Use only on the unpublished Docker network")
    args = parser.parse_args()
    load_config(args.config)
    public_url = args.public_url or os.getenv("UPI_PUBLIC_URL")
    bind = "0.0.0.0" if public_url else args.bind
    serve(args.db, args.port, bind, args.ag_ui_port, args.ag_ui_internal, public_url, args.signins)
