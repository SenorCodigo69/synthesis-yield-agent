"""Structured execution logger — captures every cycle decision as JSON.

Produces agent_log.json showing the full autonomous decision loop:
rate scans, allocation decisions, tool calls, retries, failures, and
execution outcomes.

Required for the "Let the Agent Cook" bounty (Protocol Labs / EF).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ExecutionLogger:
    """Appends structured JSON entries per cycle to data/agent_log.json.

    Each cycle produces one top-level entry with nested steps.
    File is kept bounded (last N cycles) to prevent unbounded growth.
    """

    MAX_CYCLES = 500  # Keep last 500 cycles on disk

    def __init__(self, log_path: str | None = None):
        self._path = Path(log_path) if log_path else (
            Path(__file__).resolve().parent.parent / "data" / "agent_log.json"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._current_cycle: dict[str, Any] | None = None
        self._steps: list[dict[str, Any]] = []
        self._tool_calls: list[dict[str, Any]] = []
        self._failures: list[dict[str, Any]] = []
        self._cycle_start: float = 0.0

        # Cumulative compute budget tracking
        self._total_tokens: int = 0
        self._total_api_calls: int = 0

    # ── Cycle lifecycle ──────────────────────────────────────────────

    def begin_cycle(self, cycle_num: int, mode: str = "paper") -> None:
        """Start tracking a new cycle."""
        self._cycle_start = time.monotonic()
        self._steps = []
        self._tool_calls = []
        self._failures = []
        self._current_cycle = {
            "cycle_id": cycle_num,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "failures": self._failures,
            "decisions": [],
            "executions": [],
            "final_output": {},
        }

    def end_cycle(self, summary: dict[str, Any]) -> None:
        """Finalize and persist the current cycle."""
        if self._current_cycle is None:
            return

        duration = time.monotonic() - self._cycle_start
        self._current_cycle["duration_sec"] = round(duration, 2)
        self._current_cycle["final_output"] = {
            "rates_fetched": summary.get("rates", 0),
            "protocols_eligible": summary.get("eligible", 0),
            "executions_successful": summary.get("executed", 0),
            "rebalance_signals": summary.get("signals", 0),
            "portfolio_allocated_usd": summary.get("allocated_usd", 0),
            "portfolio_reserve_usd": summary.get("reserve_usd", 0),
            "yield_accrued_usd": summary.get("yield_accrued", 0),
            "system_health": summary.get("health", "ok"),
        }
        self._current_cycle["compute_budget"] = {
            "cycle_tokens": self._total_tokens,
            "cycle_api_calls": self._total_api_calls,
            "cycle_duration_sec": round(duration, 2),
        }

        self._persist(self._current_cycle)
        self._current_cycle = None

    # ── Step tracking ────────────────────────────────────────────────

    def log_step(self, name: str, status: str = "ok", detail: str = "") -> None:
        """Record a named step in the cycle (e.g., 'scan_rates')."""
        entry = {
            "step": name,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if detail:
            entry["detail"] = detail[:500]  # Truncate to prevent bloat
        self._steps.append(entry)

    # ── Tool call tracking ───────────────────────────────────────────

    def log_tool_call(
        self,
        tool: str,
        action: str,
        result: str = "success",
        detail: str = "",
        tokens: int = 0,
        retry: bool = False,
    ) -> None:
        """Record an external tool/API call."""
        entry = {
            "tool": tool,
            "action": action,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if detail:
            entry["detail"] = detail[:500]
        if tokens:
            entry["tokens_used"] = tokens
            self._total_tokens += tokens
        if retry:
            entry["retry"] = True
        self._tool_calls.append(entry)
        self._total_api_calls += 1

    # ── Decision tracking ────────────────────────────────────────────

    def log_decision(
        self,
        decision_type: str,
        outcome: str,
        reasoning: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a decision (allocate, rebalance, withdraw, swap, etc.)."""
        if self._current_cycle is None:
            return
        entry = {
            "type": decision_type,
            "outcome": outcome,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if reasoning:
            entry["reasoning"] = reasoning[:500]
        if data:
            entry["data"] = _safe_serialize(data)
        self._current_cycle["decisions"].append(entry)

    # ── Execution tracking ────────────────────────────────────────────

    def log_execution(
        self,
        protocol: str,
        action: str,
        amount_usd: float,
        status: str,
        tx_hash: str = "",
        detail: str = "",
    ) -> None:
        """Record a deposit, withdrawal, swap, or LP operation."""
        if self._current_cycle is None:
            return
        entry = {
            "protocol": protocol,
            "action": action,
            "amount_usd": round(amount_usd, 2),
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if tx_hash:
            entry["tx_hash"] = tx_hash
        if detail:
            entry["detail"] = detail[:500]
        self._current_cycle["executions"].append(entry)

    # ── Failure tracking ─────────────────────────────────────────────

    def log_failure(self, component: str, error: str, recoverable: bool = True) -> None:
        """Record a failure (API timeout, data fetch error, etc.)."""
        self._failures.append({
            "component": component,
            "error": error[:300],
            "recoverable": recoverable,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ── Persistence ──────────────────────────────────────────────────

    def _persist(self, cycle_entry: dict) -> None:
        """Append cycle to log file, keeping bounded size."""
        try:
            existing: list[dict] = []
            if self._path.exists():
                try:
                    raw = self._path.read_text()
                    if raw.strip():
                        existing = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    existing = []

            existing.append(cycle_entry)

            # Bound the log
            if len(existing) > self.MAX_CYCLES:
                existing = existing[-self.MAX_CYCLES:]

            self._path.write_text(json.dumps(existing, indent=2, default=str))
        except Exception as e:
            logger.warning("ExecutionLogger: failed to persist cycle log: %s", e)

    # ── Read API (for dashboard / submission) ────────────────────────

    def get_recent_cycles(self, n: int = 20) -> list[dict]:
        """Return the last N logged cycles."""
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                return data[-n:]
        except Exception:
            pass
        return []

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate stats across all logged cycles."""
        cycles = self.get_recent_cycles(self.MAX_CYCLES)
        if not cycles:
            return {"total_cycles": 0}

        total_rates = sum(c.get("final_output", {}).get("rates_fetched", 0) for c in cycles)
        total_execs = sum(c.get("final_output", {}).get("executions_successful", 0) for c in cycles)
        total_failures = sum(len(c.get("failures", [])) for c in cycles)
        total_tool_calls = sum(len(c.get("tool_calls", [])) for c in cycles)
        total_yield = sum(c.get("final_output", {}).get("yield_accrued_usd", 0) for c in cycles)

        durations = [c.get("duration_sec", 0) for c in cycles if c.get("duration_sec")]
        avg_duration = sum(durations) / len(durations) if durations else 0

        return {
            "total_cycles": len(cycles),
            "total_rates_fetched": total_rates,
            "total_executions": total_execs,
            "total_failures": total_failures,
            "total_tool_calls": total_tool_calls,
            "total_yield_usd": round(total_yield, 6),
            "avg_cycle_duration_sec": round(avg_duration, 2),
        }


def _safe_serialize(data: dict) -> dict:
    """Sanitize data dict for JSON serialization — truncate large values."""
    out = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + "..."
        elif isinstance(v, (int, float, bool, type(None))):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _safe_serialize(v)
        elif isinstance(v, (list, tuple)):
            out[k] = v[:20]  # Cap list size
        else:
            out[k] = str(v)[:200]
    return out
