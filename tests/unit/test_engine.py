"""Unit tests for FaultEngine."""

import os
import tempfile
import threading
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from faultstorm.config import TestConfig
from faultstorm.faults.actions import FaultAction, FaultRegistry, WaitAction
from faultstorm.faults.engine import FaultEngine


# ---------------------------------------------------------------------------
# Helpers: lightweight stub actions for testing without Docker/iptables
# ---------------------------------------------------------------------------

class StubAction(FaultAction):
    """Non-healable, non-destructive stub action."""

    name = "stub"
    healable = False
    destructive = False

    def __init__(self, db_nodes, extra_nodes, ordinal=0, **kwargs):
        super().__init__(db_nodes, extra_nodes, ordinal, **kwargs)
        self.executed = False
        self.healed = False

    def execute(self, stop_event=None):
        self.executed = True

    def heal(self):
        self.healed = True

    def serialize(self):
        return str(self.ordinal)

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        ordinal = int(params.strip()) if params.strip() else 0
        return cls(db_nodes, extra_nodes, ordinal, **kwargs)


class HealableStubAction(FaultAction):
    """Healable, non-destructive stub action."""

    name = "healable_stub"
    healable = True
    destructive = False

    def __init__(self, db_nodes, extra_nodes, ordinal=0, **kwargs):
        super().__init__(db_nodes, extra_nodes, ordinal, **kwargs)
        self.executed = False
        self.healed = False

    def execute(self, stop_event=None):
        self.executed = True

    def heal(self):
        self.healed = True

    def serialize(self):
        return str(self.ordinal)

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        ordinal = int(params.strip()) if params.strip() else 0
        return cls(db_nodes, extra_nodes, ordinal, **kwargs)


class DestructiveStubAction(FaultAction):
    """Non-healable, destructive stub action."""

    name = "destructive_stub"
    healable = False
    destructive = True

    def __init__(self, db_nodes, extra_nodes, ordinal=0, **kwargs):
        super().__init__(db_nodes, extra_nodes, ordinal, **kwargs)
        self.executed = False
        self.healed = False

    def execute(self, stop_event=None):
        self.executed = True

    def heal(self):
        self.healed = True

    def serialize(self):
        return str(self.ordinal)

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        ordinal = int(params.strip()) if params.strip() else 0
        return cls(db_nodes, extra_nodes, ordinal, **kwargs)


class HostTargetableStubAction(FaultAction):
    """Healable, host-targetable stub action."""

    name = "host_targetable_stub"
    healable = True
    destructive = False
    host_targetable = True

    def __init__(self, db_nodes, extra_nodes, ordinal=0, node=None, **kwargs):
        super().__init__(db_nodes, extra_nodes, ordinal, **kwargs)
        self.node = node
        self.executed = False
        self.healed = False

    def execute(self, stop_event=None):
        self.executed = True

    def heal(self):
        self.healed = True

    def serialize(self):
        return f"{self.ordinal} {self.node or 'none'}"

    @classmethod
    def deserialize(cls, params, db_nodes, extra_nodes, **kwargs):
        parts = params.strip().split()
        ordinal = int(parts[0])
        node = parts[1] if len(parts) > 1 and parts[1] != "none" else None
        return cls(db_nodes, extra_nodes, ordinal, node=node, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_registry(*action_classes):
    """Build a FaultRegistry from given action classes."""
    registry = FaultRegistry()
    registry.register(WaitAction)
    for cls in action_classes:
        registry.register(cls)
    return registry


def _make_config(**overrides):
    """Build a minimal TestConfig for unit tests."""
    defaults = dict(
        name="test",
        db_nodes=["db1", "db2", "db3"],
        extra_nodes=["zk1"],
        fault_types=["stub"],
        write_phase_duration=1,
        read_phase_duration=1,
        fault_active_duration=0,
        fault_pause_duration=0,
        complex_faults_enabled=False,
        complex_fault_min_wait=0,
        complex_fault_max_wait=0,
        parallel_faults_count=1,
        max_destructive_actions=None,
    )
    defaults.update(overrides)
    return TestConfig(**defaults)


# ---------------------------------------------------------------------------
# Tests: _parse_log
# ---------------------------------------------------------------------------

class TestParseLog:
    """Tests for FaultEngine._parse_log()."""

    def test_parse_simple_actions(self, tmp_path):
        """Parse fire-and-forget and healable actions."""
        log = tmp_path / "scenario.log"
        log.write_text(
            "# FaultStorm scenario log\n"
            "\n"
            "stub 1\n"
            "+healable_stub 2\n"
            "-healable_stub 2\n"
        )
        registry = _make_registry(StubAction, HealableStubAction)
        config = _make_config(fault_types=["stub", "healable_stub"])
        engine = FaultEngine(config, registry)

        results = engine._parse_log(str(log))

        assert len(results) == 3
        assert isinstance(results[0][0], StubAction)
        assert results[0][1] is False  # not a heal
        assert isinstance(results[1][0], HealableStubAction)
        assert results[1][1] is False  # enable
        assert isinstance(results[2][0], HealableStubAction)
        assert results[2][1] is True  # heal

    def test_parse_with_timestamps(self, tmp_path):
        """Parse log lines with timestamp prefixes."""
        log = tmp_path / "scenario.log"
        log.write_text(
            "[2026-06-23T14:00:10.123] stub 1\n"
            "[2026-06-23T14:00:10.456] +healable_stub 2\n"
        )
        registry = _make_registry(StubAction, HealableStubAction)
        config = _make_config(fault_types=["stub", "healable_stub"])
        engine = FaultEngine(config, registry)

        results = engine._parse_log(str(log))

        assert len(results) == 2
        assert results[0][0].ordinal == 1
        assert results[1][0].ordinal == 2

    def test_parse_skips_comments_and_blanks(self, tmp_path):
        """Comments and blank lines are ignored."""
        log = tmp_path / "scenario.log"
        log.write_text(
            "# header\n"
            "#\n"
            "\n"
            "  \n"
            "stub 1\n"
        )
        registry = _make_registry(StubAction)
        config = _make_config()
        engine = FaultEngine(config, registry)

        results = engine._parse_log(str(log))

        assert len(results) == 1

    def test_parse_unknown_action_raises(self, tmp_path):
        """Unknown action name raises ValueError."""
        log = tmp_path / "scenario.log"
        log.write_text("unknown_action 1\n")

        registry = _make_registry(StubAction)
        config = _make_config()
        engine = FaultEngine(config, registry)

        with pytest.raises(ValueError, match="unknown action 'unknown_action'"):
            engine._parse_log(str(log))

    def test_parse_wait_action(self, tmp_path):
        """Parse a wait action line."""
        log = tmp_path / "scenario.log"
        log.write_text("wait 5 30\n")

        registry = _make_registry(StubAction)
        config = _make_config()
        engine = FaultEngine(config, registry)

        results = engine._parse_log(str(log))

        assert len(results) == 1
        action, is_heal = results[0]
        assert isinstance(action, WaitAction)
        assert action.ordinal == 5
        assert action.seconds == 30
        assert is_heal is False


# ---------------------------------------------------------------------------
# Tests: _filter_by_destructive_limit
# ---------------------------------------------------------------------------

class TestDestructiveLimit:
    """Tests for FaultEngine._filter_by_destructive_limit()."""

    def test_no_limit_returns_all(self):
        """With no limit set, all classes are returned."""
        config = _make_config(max_destructive_actions=None)
        registry = _make_registry(StubAction, DestructiveStubAction)
        engine = FaultEngine(config, registry)

        classes = [StubAction, DestructiveStubAction]
        assert engine._filter_by_destructive_limit(classes) == classes

    def test_under_limit_returns_all(self):
        """Under the limit, all classes are returned."""
        config = _make_config(max_destructive_actions=2)
        registry = _make_registry(StubAction, DestructiveStubAction)
        engine = FaultEngine(config, registry)
        engine._destructive_count = 1

        classes = [StubAction, DestructiveStubAction]
        assert engine._filter_by_destructive_limit(classes) == classes

    def test_at_limit_filters_destructive(self):
        """At the limit, destructive classes are removed."""
        config = _make_config(max_destructive_actions=1)
        registry = _make_registry(StubAction, DestructiveStubAction)
        engine = FaultEngine(config, registry)
        engine._destructive_count = 1

        classes = [StubAction, DestructiveStubAction]
        filtered = engine._filter_by_destructive_limit(classes)
        assert filtered == [StubAction]

    def test_all_destructive_at_limit_returns_empty(self):
        """If all classes are destructive and limit is reached, empty list."""
        config = _make_config(max_destructive_actions=0)
        registry = _make_registry(DestructiveStubAction)
        engine = FaultEngine(config, registry)

        classes = [DestructiveStubAction]
        assert engine._filter_by_destructive_limit(classes) == []


# ---------------------------------------------------------------------------
# Tests: _heal_all_active
# ---------------------------------------------------------------------------

class TestHealAllActive:
    """Tests for FaultEngine._heal_all_active()."""

    def test_heals_all_actions(self):
        """All actions in _active_faults get heal() called."""
        config = _make_config()
        registry = _make_registry(StubAction, HealableStubAction)
        engine = FaultEngine(config, registry)

        stub = StubAction(["db1"], ["zk1"], ordinal=1)
        healable = HealableStubAction(["db1"], ["zk1"], ordinal=2)
        engine._active_faults = [stub, healable]

        engine._heal_all_active()

        assert stub.healed is True
        assert healable.healed is True
        assert engine._active_faults == []

    def test_decrements_destructive_count(self):
        """Destructive count is decremented for destructive actions."""
        config = _make_config()
        registry = _make_registry(DestructiveStubAction)
        engine = FaultEngine(config, registry)
        engine._destructive_count = 2

        d1 = DestructiveStubAction(["db1"], ["zk1"], ordinal=1)
        d2 = DestructiveStubAction(["db1"], ["zk1"], ordinal=2)
        engine._active_faults = [d1, d2]

        engine._heal_all_active()

        assert engine._destructive_count == 0

    def test_non_healable_no_log_entry(self, tmp_path):
        """Non-healable actions don't produce heal log entries."""
        config = _make_config()
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        stub = StubAction(["db1"], ["zk1"], ordinal=1)
        engine._active_faults = [stub]
        engine._heal_all_active()
        engine._close_log()

        content = open(log_path).read()
        # Should not contain "-stub" (heal entry for non-healable)
        assert "-stub" not in content

    def test_healable_produces_log_and_wait(self, tmp_path):
        """Healable actions produce log entry and wait in heal phase."""
        config = _make_config()
        registry = _make_registry(HealableStubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        healable = HealableStubAction(["db1"], ["zk1"], ordinal=1)
        engine._active_faults = [healable]
        engine._heal_all_active()
        engine._close_log()

        content = open(log_path).read()
        assert "-healable_stub" in content
        assert "wait" in content


# ---------------------------------------------------------------------------
# Tests: _inject_complex_fault (via _active_faults tracking)
# ---------------------------------------------------------------------------

class TestInjectComplexFault:
    """Tests for _inject_complex_fault and active_faults tracking."""

    def test_all_actions_added_to_active_faults(self, tmp_path):
        """Both healable and non-healable actions are in _active_faults."""
        config = _make_config(
            fault_types=["stub"],
            complex_faults_enabled=False,
        )
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        engine._inject_complex_fault(
            config.db_nodes, config.extra_nodes,
            config.load_node, {},
            [StubAction], [],
        )
        engine._close_log()

        assert len(engine._active_faults) == 1
        assert isinstance(engine._active_faults[0], StubAction)

    def test_destructive_count_incremented(self, tmp_path):
        """Destructive action increments _destructive_count."""
        config = _make_config(
            fault_types=["destructive_stub"],
            max_destructive_actions=5,
            complex_faults_enabled=False,
        )
        registry = _make_registry(DestructiveStubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        engine._inject_complex_fault(
            config.db_nodes, config.extra_nodes,
            config.load_node, {},
            [DestructiveStubAction], [],
        )
        engine._close_log()

        assert engine._destructive_count == 1


# ---------------------------------------------------------------------------
# Tests: run_random (integration-level, with immediate stop)
# ---------------------------------------------------------------------------

class TestRunRandom:
    """Tests for FaultEngine.run_random()."""

    def test_run_random_creates_scenario_log(self, tmp_path):
        """run_random creates a scenario log file."""
        config = _make_config(
            fault_types=["stub"],
            fault_active_duration=0,
            fault_pause_duration=0,
            complex_faults_enabled=False,
        )
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        # Stop immediately via a short duration
        engine.run_random(duration=1, scenario_path=log_path)

        assert os.path.exists(log_path)
        content = open(log_path).read()
        assert "FaultStorm scenario log" in content

    def test_run_random_executes_faults(self, tmp_path):
        """run_random executes at least one fault."""
        config = _make_config(
            fault_types=["stub"],
            fault_active_duration=0,
            fault_pause_duration=0,
            complex_faults_enabled=False,
        )
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine.run_random(duration=1, scenario_path=log_path)

        content = open(log_path).read()
        assert "stub" in content

    def test_run_random_stop_interrupts(self, tmp_path):
        """Calling stop() interrupts run_random."""
        config = _make_config(
            fault_types=["stub"],
            fault_active_duration=60,
            fault_pause_duration=60,
            complex_faults_enabled=False,
        )
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")

        def run():
            engine.run_random(duration=300, scenario_path=log_path)

        t = threading.Thread(target=run)
        t.start()
        # Give it a moment to start, then stop
        engine.stop()
        t.join(timeout=5)
        assert not t.is_alive(), "Engine did not stop in time"


# ---------------------------------------------------------------------------
# Tests: run_replay
# ---------------------------------------------------------------------------

class TestRunReplay:
    """Tests for FaultEngine.run_replay()."""

    def test_replay_executes_actions(self, tmp_path):
        """Replay mode executes and heals actions from log."""
        source = tmp_path / "source.log"
        source.write_text(
            "stub 1\n"
            "+healable_stub 2\n"
            "-healable_stub 2\n"
        )

        config = _make_config(fault_types=["stub", "healable_stub"])
        registry = _make_registry(StubAction, HealableStubAction)
        engine = FaultEngine(config, registry)

        out_path = str(tmp_path / "replay.log")
        engine.run_replay(str(source), out_path)

        content = open(out_path).read()
        assert "stub" in content
        assert "-healable_stub" in content

    def test_replay_stop_interrupts(self, tmp_path):
        """Calling stop() interrupts replay."""
        # Create a long log with many waits (1 second each so stop can
        # fire mid-execution).  run_replay() clears _stop_event, so we
        # use a timer to call stop() shortly after replay begins.
        lines = ["wait {} 1\n".format(i) for i in range(1, 100)]
        source = tmp_path / "source.log"
        source.write_text("".join(lines))

        config = _make_config()
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        out_path = str(tmp_path / "replay.log")
        timer = threading.Timer(0.1, engine.stop)
        timer.start()
        engine.run_replay(str(source), out_path)

        content = open(out_path).read()
        lines_written = [l for l in content.strip().split("\n") if l and not l.startswith("#")]
        # Should have stopped well before all 99 actions were executed
        assert len(lines_written) < 10

    def test_replay_unknown_action_raises(self, tmp_path):
        """Replay raises ValueError for unknown actions."""
        source = tmp_path / "source.log"
        source.write_text("nonexistent_action 1\n")

        config = _make_config()
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        out_path = str(tmp_path / "replay.log")
        with pytest.raises(ValueError, match="unknown action"):
            engine.run_replay(str(source), out_path)


# ---------------------------------------------------------------------------
# Tests: scenario log I/O
# ---------------------------------------------------------------------------

class TestScenarioLog:
    """Tests for scenario log writing and format."""

    def test_write_healable_action_prefix(self, tmp_path):
        """Healable actions get +/- prefix in log."""
        config = _make_config()
        registry = _make_registry(HealableStubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        action = HealableStubAction(["db1"], ["zk1"], ordinal=1)
        engine._write_action(action, healing=False)
        engine._write_action(action, healing=True)
        engine._close_log()

        content = open(log_path).read()
        lines = [l for l in content.split("\n") if "healable_stub" in l]
        assert len(lines) == 2
        assert "+healable_stub" in lines[0]
        assert "-healable_stub" in lines[1]

    def test_write_non_healable_no_prefix(self, tmp_path):
        """Non-healable actions have no prefix."""
        config = _make_config()
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)

        action = StubAction(["db1"], ["zk1"], ordinal=1)
        engine._write_action(action, healing=False)
        engine._close_log()

        content = open(log_path).read()
        lines = [l for l in content.split("\n") if "stub" in l and "healable" not in l]
        assert len(lines) == 1
        assert "+stub" not in lines[0]
        assert "-stub" not in lines[0]

    def test_log_header(self, tmp_path):
        """Log file starts with header comments."""
        config = _make_config()
        registry = _make_registry(StubAction)
        engine = FaultEngine(config, registry)

        log_path = str(tmp_path / "scenario.log")
        engine._open_log(log_path)
        engine._close_log()

        content = open(log_path).read()
        assert content.startswith("# FaultStorm scenario log\n")
        assert "# Replay with:" in content
