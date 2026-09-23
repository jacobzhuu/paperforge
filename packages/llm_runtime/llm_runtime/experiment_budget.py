"""Conservative cross-process CNY reservations for explicitly scoped paid experiments.

Unknown pricing and unresolved calls never release reservations. This is a configured-price
admission ceiling, not provider invoice reconciliation. Normal production calls are unaffected.
"""

from __future__ import annotations

import math
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

_active: ContextVar[ExperimentBudget | None] = ContextVar("experiment_budget", default=None)
PHASE_CAPS = {"calibration": 50, "retrieval": 100, "generation": 250, "retest": 100}


class ExperimentBudgetExceeded(BaseException):
    """Escape draft-first degradation so exhaustion cannot masquerade as a completed run."""


class ExperimentBudget:
    def __init__(self, path: Path, phase: str, *, currency: str = "CNY"):
        if phase not in PHASE_CAPS or currency != "CNY":
            raise ValueError("explicit CNY model pricing and a registered phase are required")
        self.path, self.phase = Path(path), phase
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, phase TEXT NOT NULL, model TEXT NOT NULL,
                micro_cny INTEGER NOT NULL CHECK(micro_cny>0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        self.path.chmod(0o600)

    def reserve(self, request, config) -> str:
        price = config.price_for_model(request.model)
        if price is None or not all(
            math.isfinite(p) and p >= 0
            for p in (
                price.input_per_mtok,
                price.output_per_mtok,
            )
        ):
            raise ExperimentBudgetExceeded("unknown model price")
        # UTF-8 bytes + message-envelope allowance conservatively bound text token counts.
        input_bound = len((request.system_prompt + request.user_prompt).encode()) + 4096
        output_bound = max(0, request.max_output_tokens)
        amount = (
            Decimal(input_bound) * Decimal(str(price.input_per_mtok))
            + Decimal(output_bound) * Decimal(str(price.output_per_mtok))
        ) * (max(0, config.max_retries) + 1)
        micro_cny = max(1, int(amount.to_integral_value(rounding=ROUND_CEILING)))
        identifier = str(uuid.uuid4())
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute("SELECT COALESCE(SUM(micro_cny),0) FROM reservations").fetchone()[0]
            phase = db.execute(
                "SELECT COALESCE(SUM(micro_cny),0) FROM reservations WHERE phase=?", (self.phase,)
            ).fetchone()[0]
            if (
                total + micro_cny > 500_000_000
                or phase + micro_cny > PHASE_CAPS[self.phase] * 1_000_000
            ):
                raise ExperimentBudgetExceeded("experiment CNY reservation ceiling reached")
            db.execute(
                "INSERT INTO reservations(id,phase,model,micro_cny) VALUES (?,?,?,?)",
                (identifier, self.phase, request.model, micro_cny),
            )
        return identifier

    def report(self):
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT phase,COUNT(*),SUM(micro_cny) FROM reservations GROUP BY phase"
            )
            return {
                phase: {"calls": count, "reserved_cny": micro / 1_000_000}
                for phase, count, micro in rows
            }

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)


def reserve_experiment(request, config) -> str | None:
    budget = _active.get()
    return budget.reserve(request, config) if budget else None
