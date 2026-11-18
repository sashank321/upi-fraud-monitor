"""Transactional queue and ledger writes for autonomous simulated payments."""

import json
import time
from uuid import uuid4

from payments import ORCHESTRATION_POLICY, PAYER, PaymentError, Store


PAYMENT_SECONDS = 240
RUN_LEASE_SECONDS = 150


class AutonomousStore(Store):
    autonomous = True

    def queue_transfer(self, db, payee, amount, request_key, now):
        transfer_id = uuid4().hex
        evidence = self.collect_evidence(db, payee, amount, now, legacy_signals=False)
        db.execute(
            """INSERT INTO transfers(id, payer, payee, amount, request_key, status, created_at,
                       expires_at, policy_version, evidence)
               VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?, ?)""",
            (transfer_id, PAYER, payee, amount, request_key, now, now + PAYMENT_SECONDS,
             ORCHESTRATION_POLICY, json.dumps(evidence)),
        )
        self.post(db, transfer_id, "reserve", (PAYER, "available"), (PAYER, "reserved"), amount)
        db.execute("INSERT INTO payment_runs(transfer_id, phase) VALUES (?, 'screening')", (transfer_id,))
        return transfer_id

    def claim_run(self):
        with self.transaction() as db:
            now = time.time()
            row = db.execute(
                """SELECT r.transfer_id FROM payment_runs AS r JOIN transfers AS t ON t.id = r.transfer_id
                   WHERE r.phase IN ('screening', 'deliberating') AND (r.leased_until IS NULL OR r.leased_until <= ?)
                     AND t.status = 'held' AND t.expires_at > ?
                   ORDER BY t.created_at LIMIT 1""", (now, now),
            ).fetchone()
            if row is None:
                return None
            transfer_id = row["transfer_id"]
            token = uuid4().hex
            db.execute("UPDATE payment_runs SET lease_token = ?, leased_until = ? WHERE transfer_id = ?",
                       (token, now + RUN_LEASE_SECONDS, transfer_id))
            return transfer_id, token

    def dialogue(self, transfer_id):
        case = self.investigation_case(transfer_id, include_journal=True)
        if case is None:
            return None
        with self.connection() as db:
            rows = db.execute(
                """SELECT sequence, stage, agent, model, audience, decision, summary, created_at, assessment
                   FROM payment_turns WHERE transfer_id = ? ORDER BY sequence""", (transfer_id,),
            ).fetchall()
        case["turns"] = [dict(row) for row in rows]
        for turn in case["turns"]:
            turn["assessment"] = json.loads(turn["assessment"])
        return case

    @staticmethod
    def insert_turn(db, transfer_id, turn, now):
        db.execute(
            """INSERT INTO payment_turns(transfer_id, stage, agent, model, audience, decision, summary, created_at, assessment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (transfer_id, turn["stage"], turn["agent"], turn["model"], turn["audience"],
             turn["decision"], turn["summary"], now, json.dumps(turn.get("assessment", {}))),
        )

    def publish_turn(self, transfer_id, token, turn):
        with self.transaction() as db:
            now = time.time()
            row = db.execute(
                """SELECT t.*, r.phase FROM transfers AS t JOIN payment_runs AS r ON r.transfer_id = t.id
                   WHERE t.id = ? AND t.status = 'held' AND t.expires_at > ?
                     AND r.lease_token = ? AND r.leased_until > ?""", (transfer_id, now, token, now),
            ).fetchone()
            if not row:
                return False
            stage, decision = turn["stage"], turn["decision"]
            if stage == "screen" and row["phase"] != "screening":
                return False
            if stage in {"review", "final"} and row["phase"] != "deliberating":
                return False
            self.insert_turn(db, transfer_id, turn, now)
            if stage == "screen":
                evidence = json.loads(row["evidence"])
                evidence["reasons"] = [turn["summary"]] if decision == "hold" else []
                db.execute("UPDATE transfers SET evidence = ? WHERE id = ?", (json.dumps(evidence), transfer_id))
                if decision == "hold":
                    db.execute("UPDATE payment_runs SET phase = 'deliberating', considered_hold = 1 WHERE transfer_id = ?",
                               (transfer_id,))
                else:
                    self.finish_run(db, row, "settled" if decision == "clear" else "cancelled", now, turn["summary"])
            elif stage == "final":
                self.finish_run(db, row, "settled" if decision == "settle" else "cancelled", now, turn["summary"])
            return True

    def finish_run(self, db, row, status, now, summary):
        self.finish(db, row, status, now, summary)
        db.execute(
            "UPDATE payment_runs SET phase = ?, lease_token = NULL, leased_until = NULL WHERE transfer_id = ?",
            ("completed" if status == "settled" else "reversed", row["id"]),
        )

    def expire(self):
        with self.transaction() as db:
            now = time.time()
            for row in db.execute(
                "SELECT * FROM transfers WHERE status = 'held' AND expires_at <= ? LIMIT 50", (now,),
            ).fetchall():
                if row["policy_version"] == ORCHESTRATION_POLICY:
                    summary = "The decision deadline expired. The reservation was returned to the sender."
                    self.insert_turn(db, row["id"], {"stage": "final", "agent": "system", "model": None,
                                                   "audience": "Ledger", "decision": "reverse", "summary": summary}, now)
                    self.finish_run(db, row, "expired", now, summary)
                else:
                    self.finish(db, row, "expired", now, "Verification window expired.")

    def resolve(self, transfer_id, action, note, verified):
        raise PaymentError("This workspace settles and reverses payments automatically.", 409)
