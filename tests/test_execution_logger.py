"""Tests for the structured execution logger."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.execution_logger import ExecutionLogger, _safe_serialize


class TestExecutionLogger:
    def test_begin_end_cycle(self, tmp_path):
        log_file = tmp_path / "test_log.json"
        el = ExecutionLogger(str(log_file))

        el.begin_cycle(1, mode="paper")
        el.end_cycle({"rates": 3, "eligible": 2, "executed": 1})

        data = json.loads(log_file.read_text())
        assert len(data) == 1
        assert data[0]["cycle_id"] == 1
        assert data[0]["mode"] == "paper"
        assert data[0]["final_output"]["rates_fetched"] == 3
        assert data[0]["duration_sec"] >= 0

    def test_log_step(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_step("scan_rates", "ok", "3 rates fetched")
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        steps = data[0]["steps"]
        assert len(steps) == 1
        assert steps[0]["step"] == "scan_rates"
        assert steps[0]["status"] == "ok"
        assert steps[0]["detail"] == "3 rates fetched"

    def test_log_tool_call(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_tool_call("defillama", "fetch_rates", tokens=100)
        el.log_tool_call("coingecko", "usdc_price", result="success")
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        calls = data[0]["tool_calls"]
        assert len(calls) == 2
        assert calls[0]["tool"] == "defillama"
        assert calls[0]["tokens_used"] == 100
        assert data[0]["compute_budget"]["cycle_tokens"] == 100
        assert data[0]["compute_budget"]["cycle_api_calls"] == 2

    def test_log_decision(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_decision("allocation", "computed", reasoning="best yield on aave",
                         data={"eligible": 2, "total": 10000})
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        decisions = data[0]["decisions"]
        assert len(decisions) == 1
        assert decisions[0]["type"] == "allocation"
        assert decisions[0]["outcome"] == "computed"
        assert decisions[0]["reasoning"] == "best yield on aave"
        assert decisions[0]["data"]["eligible"] == 2

    def test_log_execution(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_execution("aave-v3", "deposit", 5000.0, "success", tx_hash="0xabc")
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        execs = data[0]["executions"]
        assert len(execs) == 1
        assert execs[0]["protocol"] == "aave-v3"
        assert execs[0]["action"] == "deposit"
        assert execs[0]["amount_usd"] == 5000.0
        assert execs[0]["tx_hash"] == "0xabc"

    def test_log_failure(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_failure("defillama", "timeout after 20s", recoverable=True)
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        failures = data[0]["failures"]
        assert len(failures) == 1
        assert failures[0]["component"] == "defillama"
        assert failures[0]["recoverable"] is True

    def test_bounded_cycles(self, tmp_path):
        log_file = tmp_path / "log.json"
        el = ExecutionLogger(str(log_file))
        el.MAX_CYCLES = 5  # Override for test

        for i in range(10):
            el.begin_cycle(i + 1)
            el.end_cycle({})

        data = json.loads(log_file.read_text())
        assert len(data) == 5
        assert data[0]["cycle_id"] == 6  # Oldest kept
        assert data[-1]["cycle_id"] == 10  # Newest

    def test_corrupt_file_recovery(self, tmp_path):
        log_file = tmp_path / "log.json"
        log_file.write_text("not valid json {{{")

        el = ExecutionLogger(str(log_file))
        el.begin_cycle(1)
        el.end_cycle({})

        data = json.loads(log_file.read_text())
        assert len(data) == 1  # Recovered, started fresh

    def test_no_cycle_ignores_decision(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        # No begin_cycle called
        el.log_decision("test", "outcome")  # Should not crash
        el.log_execution("aave", "deposit", 100, "ok")  # Should not crash

    def test_get_recent_cycles(self, tmp_path):
        log_file = tmp_path / "log.json"
        el = ExecutionLogger(str(log_file))

        for i in range(5):
            el.begin_cycle(i + 1)
            el.end_cycle({"rates": i + 1})

        recent = el.get_recent_cycles(3)
        assert len(recent) == 3
        assert recent[0]["cycle_id"] == 3
        assert recent[-1]["cycle_id"] == 5

    def test_get_stats(self, tmp_path):
        log_file = tmp_path / "log.json"
        el = ExecutionLogger(str(log_file))

        el.begin_cycle(1)
        el.log_tool_call("api", "test")
        el.end_cycle({"rates": 3, "executed": 1, "yield_accrued": 0.05})

        el.begin_cycle(2)
        el.log_tool_call("api", "test")
        el.log_failure("test", "error")
        el.end_cycle({"rates": 3, "executed": 2, "yield_accrued": 0.10})

        stats = el.get_stats()
        assert stats["total_cycles"] == 2
        assert stats["total_rates_fetched"] == 6
        assert stats["total_executions"] == 3
        assert stats["total_failures"] == 1
        assert stats["total_tool_calls"] == 2
        assert stats["total_yield_usd"] == 0.15

    def test_get_stats_empty(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "nonexistent.json"))
        stats = el.get_stats()
        assert stats == {"total_cycles": 0}

    def test_detail_truncation(self, tmp_path):
        el = ExecutionLogger(str(tmp_path / "log.json"))
        el.begin_cycle(1)
        el.log_step("test", detail="x" * 1000)
        el.end_cycle({})

        data = json.loads((tmp_path / "log.json").read_text())
        assert len(data[0]["steps"][0]["detail"]) == 500

    def test_creates_data_dir(self, tmp_path):
        log_file = tmp_path / "subdir" / "deep" / "log.json"
        el = ExecutionLogger(str(log_file))
        assert log_file.parent.exists()


class TestSafeSerialize:
    def test_truncates_long_strings(self):
        result = _safe_serialize({"key": "x" * 500})
        assert len(result["key"]) == 303  # 300 + "..."

    def test_preserves_numbers(self):
        result = _safe_serialize({"a": 42, "b": 3.14, "c": True, "d": None})
        assert result == {"a": 42, "b": 3.14, "c": True, "d": None}

    def test_caps_lists(self):
        result = _safe_serialize({"items": list(range(50))})
        assert len(result["items"]) == 20

    def test_nested_dicts(self):
        result = _safe_serialize({"outer": {"inner": "value"}})
        assert result["outer"]["inner"] == "value"

    def test_converts_unknown_types(self):
        result = _safe_serialize({"obj": set([1, 2, 3])})
        assert isinstance(result["obj"], str)
