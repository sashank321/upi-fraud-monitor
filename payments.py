from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from statistics import median
import time
from uuid import uuid4


SCENARIOS = (
    {"id": "routine", "title": "Routine payment", "payee": "canteen", "amount": 45000},
    {"id": "suspicious", "title": "Unfamiliar recipient", "payee": "new-recipient", "amount": 1850000},
    {"id": "legitimate", "title": "First rent payment", "payee": "rent", "amount": 1800000},
)
PAYER = "demo-payer"
POLICY_VERSION = "rules-v1"
MODEL_POLICY_VERSION = "model-and-verification-v1"
HOLD_SECONDS = 300
LEASE_SECONDS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    available INTEGER NOT NULL CHECK (available >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0)
);
CREATE TABLE IF NOT EXISTS prior_payments (
    payer TEXT NOT NULL,
    payee TEXT NOT NULL,
    amount INTEGER NOT NULL,
    settled_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY,
    payer TEXT NOT NULL REFERENCES accounts(id),
    payee TEXT NOT NULL REFERENCES accounts(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    request_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('held', 'settled', 'cancelled', 'expired')),
    created_at REAL NOT NULL,
    expires_at REAL,
    resolved_at REAL,
    review_note TEXT,
    verified INTEGER CHECK (verified IN (0, 1)),
    policy_version TEXT NOT NULL,
    evidence TEXT NOT NULL,
    UNIQUE(payer, request_key)
);
CREATE INDEX IF NOT EXISTS transfer_expiry ON transfers(status, expires_at);
CREATE INDEX IF NOT EXISTS transfer_sender ON transfers(payer, status, resolved_at);
CREATE INDEX IF NOT EXISTS transfer_recipient ON transfers(payee, status, resolved_at);
CREATE TABLE IF NOT EXISTS journal (
    transfer_id TEXT NOT NULL REFERENCES transfers(id),
    operation TEXT NOT NULL,
    line INTEGER NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    bucket TEXT NOT NULL CHECK (bucket IN ('available', 'reserved')),
    delta INTEGER NOT NULL CHECK (delta <> 0),
    PRIMARY KEY (transfer_id, operation, line)
);
CREATE TABLE IF NOT EXISTS investigations (
    transfer_id TEXT PRIMARY KEY REFERENCES transfers(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'complete', 'closed')),
    lease_token TEXT,
    leased_until REAL,
    report TEXT
);
CREATE INDEX IF NOT EXISTS investigation_ready ON investigations(status, leased_until);
"""


class PaymentError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, path, risk_model=None):
        self.path = Path(path)
        self.risk_model = risk_model
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                self.seed(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    @staticmethod
    def seed(db):
        db.executemany(
            "INSERT INTO accounts(id, name, available) VALUES (?, ?, ?)",
            [(PAYER, "Demo payer", 10000000), ("canteen", "Campus Canteen", 0),
             ("new-recipient", "New recipient", 0), ("rent", "Greenview Rentals", 0)],
        )
        now = time.time()
        history = [(PAYER, "canteen", amount, now - age * 3600)
                   for age, amount in enumerate((28000, 45000, 60000, 52000), start=2)]
        history.extend((f"historical-sender-{index}", "new-recipient", 800000, now - index * 180)
                       for index in range(1, 8))
        db.executemany("INSERT INTO prior_payments VALUES (?, ?, ?, ?)", history)

    @staticmethod
    def collect_evidence(db, payee, amount, now):
        history = db.execute(
            """SELECT payee, amount FROM prior_payments WHERE payer = ? AND settled_at < ?
               UNION ALL
               SELECT payee, amount FROM transfers
               WHERE payer = ? AND status = 'settled' AND resolved_at <= ?""",
            (PAYER, now, PAYER, now),
        ).fetchall()
        cutoff = now - 86400
        inbound = db.execute(
            """SELECT COUNT(DISTINCT payer) FROM (
                   SELECT payer FROM prior_payments WHERE payee = ? AND settled_at BETWEEN ? AND ?
                   UNION ALL
                   SELECT payer FROM transfers WHERE payee = ? AND status = 'settled'
                       AND resolved_at BETWEEN ? AND ?
               )""", (payee, cutoff, now, payee, cutoff, now),
        ).fetchone()[0]
        typical = int(median(row["amount"] for row in history)) if history else None
        first_payment = not any(row["payee"] == payee for row in history)
        reasons = []
        if first_payment and amount >= 500000:
            reasons.append("Large first payment to this recipient")
        if typical and amount >= max(500000, typical * 5):
            reasons.append("Amount is much higher than the sender's typical payment")
        if inbound >= 5:
            reasons.append("Recipient received payments from several distinct senders in 24 hours")
        return {
            "reasons": reasons,
            "sender_history": {"prior_payments": len(history), "median_paise": typical,
                               "first_payment_to_recipient": first_payment},
            "recipient_activity": {"window_hours": 24, "distinct_senders": inbound},
        }

    @staticmethod
    def post(db, transfer_id, operation, source, target, amount):
        for line, ((account, bucket), delta) in enumerate(((source, -amount), (target, amount))):
            db.execute(
                """UPDATE accounts SET
                       available = available + CASE WHEN ? = 'available' THEN ? ELSE 0 END,
                       reserved = reserved + CASE WHEN ? = 'reserved' THEN ? ELSE 0 END
                   WHERE id = ?""",
                (bucket, delta, bucket, delta, account),
            )
            db.execute("INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                       (transfer_id, operation, line, account, bucket, delta))

    @staticmethod
    def serialize(row):
        item = dict(row)
        evidence = json.loads(item.pop("evidence"))
        item["reasons"] = evidence["reasons"]
        item["risk"] = evidence.get("risk")
        item.pop("request_key")
        item["report"] = json.loads(item["report"]) if item["report"] else None
        return item

    def transfer(self, payee, amount, request_key):
        if not isinstance(payee, str) or payee == PAYER:
            raise PaymentError("Choose a valid recipient.")
        if type(amount) is not int or not 1 <= amount <= 10000000:
            raise PaymentError("Provide a positive integer amount in paise, up to ₹100,000.")
        if not isinstance(request_key, str) or not 1 <= len(request_key.strip()) <= 100:
            raise PaymentError("A valid Idempotency-Key is required.")
        with self.transaction() as db:
            prior = db.execute("SELECT id, payee, amount FROM transfers WHERE payer = ? AND request_key = ?",
                               (PAYER, request_key)).fetchone()
            if prior:
                if (prior["payee"], prior["amount"]) != (payee, amount):
                    raise PaymentError("This request key already belongs to a different payment.", 409)
                return prior["id"], False
            if not db.execute("SELECT 1 FROM accounts WHERE id = ?", (payee,)).fetchone():
                raise PaymentError("Recipient does not exist.", 404)
            if db.execute("SELECT available FROM accounts WHERE id = ?", (PAYER,)).fetchone()[0] < amount:
                raise PaymentError("Insufficient available demo balance.", 409)
            now = time.time()
            evidence = self.collect_evidence(db, payee, amount, now)
            held = bool(evidence["reasons"])
            policy_version = POLICY_VERSION
            if self.risk_model is not None:
                policy_version = MODEL_POLICY_VERSION
                risk = self.risk_model.assess(amount, evidence)
                evidence["risk"] = risk
                verification = evidence["sender_history"]["first_payment_to_recipient"] and amount >= 500000
                evidence["signals"] = evidence["reasons"]
                reasons = []
                if risk["flagged"]:
                    reasons.append("Classifier score reached the review threshold")
                if verification:
                    reasons.append("Bank policy requires verification for a first payment of ₹5,000 or more")
                evidence["reasons"] = reasons
                held = bool(reasons)
            transfer_id = uuid4().hex
            db.execute(
                """INSERT INTO transfers(id, payer, payee, amount, request_key, status, created_at,
                                         expires_at, resolved_at, policy_version, evidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (transfer_id, PAYER, payee, amount, request_key, "held" if held else "settled",
                 now, now + HOLD_SECONDS if held else None, None if held else now,
                 policy_version, json.dumps(evidence)),
            )
            if held:
                self.post(db, transfer_id, "reserve", (PAYER, "available"), (PAYER, "reserved"), amount)
                db.execute("INSERT INTO investigations(transfer_id, status) VALUES (?, 'pending')",
                           (transfer_id,))
            else:
                self.post(db, transfer_id, "settle", (PAYER, "available"), (payee, "available"), amount)
            return transfer_id, True

    def finish(self, db, row, status, now, note, verified=False):
        transfer_id = row["id"]
        target = row["payee"] if status == "settled" else row["payer"]
        self.post(db, transfer_id, status, (row["payer"], "reserved"), (target, "available"), row["amount"])
        db.execute(
            "UPDATE transfers SET status = ?, resolved_at = ?, review_note = ?, verified = ? WHERE id = ?",
            (status, now, note, verified, transfer_id),
        )
        db.execute(
            """UPDATE investigations SET status = 'closed', lease_token = NULL, leased_until = NULL
               WHERE transfer_id = ? AND status IN ('pending', 'running')""", (transfer_id,),
        )

    def resolve(self, transfer_id, action, note, verified):
        if action not in ("release", "cancel"):
            raise PaymentError("Choose release or cancel.")
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 600:
            raise PaymentError("Enter a review reason between 1 and 600 characters.")
        if type(verified) is not bool or (action == "release" and not verified):
            raise PaymentError("Confirm simulated verification before releasing the payment.")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
            if row is None:
                raise PaymentError("Transfer not found.", 404)
            if row["status"] != "held":
                raise PaymentError(f'This payment is already {row["status"]}.', 409)
            now = time.time()
            expired = row["expires_at"] <= now
            if expired:
                self.finish(db, row, "expired", now, "Verification window expired.")
            else:
                self.finish(db, row, "settled" if action == "release" else "cancelled",
                            now, note.strip(), verified if action == "release" else False)
        if expired:
            raise PaymentError("The verification window expired. The reservation was released.", 409)

    def expire(self):
        with self.transaction() as db:
            now = time.time()
            rows = db.execute(
                "SELECT * FROM transfers WHERE status = 'held' AND expires_at <= ? LIMIT 50", (now,),
            ).fetchall()
            for row in rows:
                self.finish(db, row, "expired", now, "Verification window expired.")

    def claim(self):
        with self.transaction() as db:
            now = time.time()
            row = db.execute(
                """SELECT t.id, t.amount, t.evidence FROM investigations AS j
                   JOIN transfers AS t ON t.id = j.transfer_id
                   WHERE (j.status = 'pending' OR (j.status = 'running' AND j.leased_until <= ?))
                       AND t.status = 'held' AND t.expires_at > ?
                   ORDER BY t.created_at LIMIT 1""", (now, now),
            ).fetchone()
            if not row:
                return None
            token = uuid4().hex
            db.execute(
                """UPDATE investigations SET status = 'running', lease_token = ?, leased_until = ?
                   WHERE transfer_id = ?""", (token, now + LEASE_SECONDS, row["id"]),
            )
            return row["id"], token, row["amount"], json.loads(row["evidence"])

    def complete(self, transfer_id, token, report):
        with self.transaction() as db:
            db.execute(
                """UPDATE investigations SET status = 'complete', report = ?,
                       lease_token = NULL, leased_until = NULL
                   WHERE transfer_id = ? AND status = 'running' AND lease_token = ?""",
                (json.dumps(report), transfer_id, token),
            )

    def snapshot(self, recipient=None):
        with self.connection() as db:
            db.execute("BEGIN")
            accounts = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            transfers = db.execute(
                """SELECT t.*, a.name AS recipient, j.status AS investigation_status, j.report
                   FROM transfers AS t JOIN accounts AS a ON a.id = t.payee
                   LEFT JOIN investigations AS j ON j.transfer_id = t.id
                   WHERE (? IS NULL OR t.payee = ?)
                   ORDER BY (t.status = 'held') DESC, t.created_at DESC LIMIT 100""",
                (recipient, recipient),
            ).fetchall()
            journal = db.execute(
                """SELECT transfer_id, operation, account_id, bucket, delta FROM journal
                   ORDER BY rowid DESC LIMIT 100""",
            ).fetchall()
            return {
                "scenarios": SCENARIOS,
                "accounts": [dict(row) for row in accounts],
                "transfers": [self.serialize(row) for row in transfers],
                "journal": [dict(row) for row in journal],
            }

    def view(self, role, recipient=None):
        state = self.snapshot(recipient if role == "receiver" else None)
        recipients = [{"id": account["id"], "name": account["name"], "address": f'{account["id"]}@demo'}
                      for account in state["accounts"]
                      if account["id"] != PAYER]
        if role == "bank":
            return state
        details = ("id", "recipient", "payee", "amount", "status", "created_at", "resolved_at", "expires_at")
        if role == "sender":
            payer = next(account for account in state["accounts"] if account["id"] == PAYER)
            return {"account": payer, "recipients": recipients,
                    "transfers": [{key: row[key] for key in details} for row in state["transfers"]]}
        account = next((row for row in state["accounts"] if row["id"] == recipient and row["id"] != PAYER), None)
        if account is None:
            raise PaymentError("Receiver profile not found.", 404)
        return {"account": {"id": account["id"], "name": account["name"], "address": f'{recipient}@demo',
                            "available": account["available"]},
                "transfers": [{key: row[key] for key in details} for row in state["transfers"]]}
