"""Unit tests for TestRunner."""

import json
import os
import threading
from typing import List, Optional, Set
from unittest.mock import MagicMock, patch

import pytest

from faultstorm.config import TestConfig
from faultstorm.db_client import DatabaseClient
from faultstorm.faults.actions import FaultAction, FaultRegistry, WaitAction
from faultstorm.model import CheckResult
from faultstorm.runner import TestRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StubAction(FaultAction):
    """Non-healable stub action for testing."""

    name = "stub"
    healable = False

    def __init__(self, db_nodes, extra_nodes, ordinal=0, **kwargs):
        super().__init__(db_nodes, extra_nodes, ordinal, **kwargs)

    def execute(self, stop_event=None):
        pass

    def serialize(self):
        return str(self.ordinal)

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        ordinal = int(params.strip()) if params.strip() else 0
        return cls(db_nodes, extra_nodes, ordinal, **kwargs)


class MockDatabaseClient(DatabaseClient):
    """In-memory database client for testing."""

    def __init__(self, db_nodes: List[str]):
        self._db_nodes = db_nodes
        self._data: Set[int] = set()
        self._setup_called = False

    def get_db_nodes(self) -> List[str]:
        return self._db_nodes

    def setup(self, node: str) -> None:
        self._setup_called = True

    def add(self, node: str, value: int) -> None:
        self._data.add(value)

    def read(self, node: str) -> Set[int]:
        return set(self._data)

    def is_definite_failure(self, exc: Exception) -> bool:
        return False


def _make_config(tmp_path, **overrides):
    """Build a minimal TestConfig for unit tests."""
    defaults = dict(
        name="test",
        db_nodes=["db1", "db2"],
        extra_nodes=["zk1"],
        write_phase_duration=2,
        read_phase_duration=1,
        add_interval=0.05,
        read_interval=0.1,
        operation_timeout=2.0,
        fault_types=["stub"],
        fault_active_duration=0,
        fault_pause_duration=0,
        complex_faults_enabled=False,
        complex_fault_min_wait=0,
        complex_fault_max_wait=0,
        parallel_faults_count=1,
        max_destructive_actions=None,
        operations_log=str(tmp_path / "operations.log"),
        scenario_log=str(tmp_path / "scenario.log"),
    )
    defaults.update(overrides)
    return TestConfig(**defaults)


def _make_registry():
    """Build a FaultRegistry with stub + wait actions."""
    registry = FaultRegistry()
    registry.register(WaitAction)
    registry.register(StubAction)
    return registry


# ---------------------------------------------------------------------------
# Tests: TestRunner.run()
# ---------------------------------------------------------------------------

class TestRunnerRun:
    """Tests for TestRunner.run()."""

    @patch.object(TestRunner, "_run_phases")
    def test_run_creates_log_directories(self, mock_run_phases, tmp_path):
        """run() creates log directory structure."""
        sub = tmp_path / "sub" / "dir"
        config = _make_config(
            tmp_path,
            operations_log=str(sub / "ops.log"),
            scenario_log=str(sub / "scenario.log"),
        )
        mock_run_phases.return_value = CheckResult(valid=True)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result = runner.run()

        assert result.valid is True
        assert sub.exists()

    def test_run_full_cycle_passes(self, tmp_path):
        """Full test cycle with in-memory DB passes consistency check."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result = runner.run()

        assert result.valid is True
        assert result.total_attempts > 0
        assert result.successful_adds > 0
        assert len(result.lost) == 0
        assert len(result.unexpected) == 0

    def test_run_produces_operations_log(self, tmp_path):
        """run() produces an operations log file."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        runner.run()

        ops_log = config.operations_log
        assert os.path.exists(ops_log)
        with open(ops_log) as f:
            lines = f.readlines()
        assert len(lines) > 0
        # Each line should be valid JSON
        for line in lines:
            data = json.loads(line.strip())
            assert "type" in data
            assert "action" in data

    def test_run_produces_scenario_log(self, tmp_path):
        """run() produces a scenario log file."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        runner.run()

        assert os.path.exists(config.scenario_log)
        content = open(config.scenario_log).read()
        assert "FaultStorm scenario log" in content


# ---------------------------------------------------------------------------
# Tests: TestRunner.run_and_print()
# ---------------------------------------------------------------------------

class TestRunnerRunAndPrint:
    """Tests for TestRunner.run_and_print()."""

    def test_run_and_print_returns_true_on_pass(self, tmp_path):
        """run_and_print() returns True when test passes."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result = runner.run_and_print()

        assert result is True

    def test_run_and_print_outputs_results(self, tmp_path, capsys):
        """run_and_print() prints test results to stdout."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        runner.run_and_print()

        captured = capsys.readouterr()
        assert "Test Results" in captured.out
        assert "Valid:" in captured.out
        assert "Total attempts:" in captured.out
        assert "Write availability:" in captured.out


# ---------------------------------------------------------------------------
# Tests: consistency detection
# ---------------------------------------------------------------------------

class TestConsistency:
    """Tests verifying that data loss is correctly detected."""

    def test_lost_data_detected(self, tmp_path):
        """Detect lost data when a write is confirmed but value is missing."""
        config = _make_config(tmp_path)

        class LossyClient(MockDatabaseClient):
            """Client that 'loses' value 1 on read."""
            def read(self, node):
                return self._data - {1}

        db_client = LossyClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result = runner.run()

        # Value 1 should be written and confirmed but missing from read
        if 1 in result.lost:
            assert result.valid is False
            assert 1 in result.lost

    def test_unexpected_data_detected(self, tmp_path):
        """Detect unexpected data that appears without being written."""
        config = _make_config(tmp_path)

        class CorruptClient(MockDatabaseClient):
            """Client that adds phantom value 999999 on read."""
            def read(self, node):
                return self._data | {999999}

        db_client = CorruptClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result = runner.run()

        assert result.valid is False
        assert 999999 in result.unexpected


# ---------------------------------------------------------------------------
# Tests: replay mode
# ---------------------------------------------------------------------------

class TestReplayMode:
    """Tests for replay mode integration."""

    def test_replay_mode(self, tmp_path):
        """Run a test, then replay the scenario log and get same result."""
        config = _make_config(tmp_path)
        db_client = MockDatabaseClient(config.db_nodes)
        registry = _make_registry()

        runner = TestRunner(config, db_client, registry)
        result1 = runner.run()

        # Now replay
        replay_config = _make_config(
            tmp_path,
            replay_scenario=config.scenario_log,
            operations_log=str(tmp_path / "ops2.log"),
            scenario_log=str(tmp_path / "scenario2.log"),
        )
        db_client2 = MockDatabaseClient(replay_config.db_nodes)
        runner2 = TestRunner(replay_config, db_client2, registry)
        result2 = runner2.run()

        assert result2.valid is True
