"""
Fault engine for FaultStorm tests.

Two modes:
  - Random: picks random actions from a configured subset, applies a strategy
    (currently: 2 random faults → wait → heal_all → wait → repeat),
    and writes every action to a scenario log file.
  - Replay: reads a previously written scenario log, deserializes each line
    back into an action, and executes them in order.

Scenario log format (line-based)::

    # FaultStorm scenario log
    # Generated at 2026-06-23T14:00:00.000
    # Replay with: python main.py --replay-scenario <this_file>

    [2026-06-23T14:00:10.123] kill postgres postgresql1
    [2026-06-23T14:00:10.456] partition_random_node zookeeper2
    [2026-06-23T14:00:10.789] wait 60
    [2026-06-23T14:00:70.012] heal_all
    [2026-06-23T14:01:10.345] wait 60
    ...
"""

import logging
import random
import re
import threading
from datetime import datetime
from typing import List, Optional, IO

from faultstorm.config import TestConfig
from faultstorm.faults.actions import (
    FaultAction,
    FaultRegistry,
    WaitAction,
    HealAllAction,
)
from faultstorm.faults.partitioners import Partitioners

logger = logging.getLogger(__name__)

# Strip optional timestamp prefix: [2026-06-23T14:05:30.123]
_TIMESTAMP_RE = re.compile(r'^\[[\d\-T:.]+\]\s*')


def _timestamp() -> str:
    """Current timestamp for scenario lines."""
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


class FaultEngine:
    """Engine for injecting failures into the cluster.

    Supports two modes:
      - run_random(): random faults with a configurable strategy
      - run_replay(): deterministic replay from a scenario log
    """

    def __init__(self, config: TestConfig, registry: FaultRegistry):
        """Initialize fault engine.

        Args:
            config: Test configuration
            registry: Registry with all known action classes
        """
        self.config = config
        self.registry = registry
        self._stop_event = threading.Event()
        self._log_file: Optional[IO] = None

    # ---- Scenario log I/O ----

    def _open_log(self, path: str) -> None:
        """Open scenario log file for writing."""
        self._log_file = open(path, 'w')
        self._log_file.write("# FaultStorm scenario log\n")
        self._log_file.write(f"# Generated at {_timestamp()}\n")
        self._log_file.write("#\n")
        self._log_file.write("# Replay with: python main.py --replay-scenario <this_file>\n")
        self._log_file.write("\n")
        self._log_file.flush()

    def _close_log(self) -> None:
        """Close scenario log file."""
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def _write_action(self, action: FaultAction) -> None:
        """Write a single action to the scenario log.

        Format: [timestamp] action_name serialized_params

        Args:
            action: Executed action to record
        """
        if self._log_file is None:
            return
        params = action.serialize()
        if params:
            line = f"[{_timestamp()}] {action.name} {params}\n"
        else:
            line = f"[{_timestamp()}] {action.name}\n"
        self._log_file.write(line)
        self._log_file.flush()

    # ---- Random mode ----

    def run_random(self, duration: int, scenario_path: str) -> None:
        """Run random fault cycles for the specified duration.

        Strategy: pick 2 random fault actions → execute them → wait
        (fault_active_duration) → heal_all → wait (fault_pause_duration)
        → repeat.

        Args:
            duration: Total duration in seconds
            scenario_path: Path to write the scenario log
        """
        self._stop_event.clear()
        fault_classes = self.registry.get_classes(self.config.fault_types)

        self._open_log(scenario_path)
        try:
            self._random_loop(duration, fault_classes)
        finally:
            self._close_log()

    def _random_loop(self, duration: int,
                     fault_classes: List[type]) -> None:
        """Main random-mode loop."""
        db = self.config.db_nodes
        extra = self.config.extra_nodes

        while not self._stop_event.is_set():
            # 2 random faults
            for _ in range(2):
                cls = random.choice(fault_classes)
                action = cls(db, extra)
                self._execute_and_log(action)

            # Wait (active phase)
            wait_active = WaitAction(db, extra, self.config.fault_active_duration)
            self._execute_and_log(wait_active)
            if self._stop_event.is_set():
                break

            # Heal all
            heal = HealAllAction(db, extra)
            self._execute_and_log(heal)

            # Wait (pause phase)
            wait_pause = WaitAction(db, extra, self.config.fault_pause_duration)
            self._execute_and_log(wait_pause)

    def _execute_and_log(self, action: FaultAction) -> None:
        """Execute an action and write it to the scenario log."""
        try:
            action.execute(self._stop_event)
        except Exception as e:
            logger.error("Action %s failed: %s", action.name, e)
        self._write_action(action)

    # ---- Replay mode ----

    def run_replay(self, replay_path: str, scenario_path: str) -> None:
        """Replay a previously recorded scenario log.

        Args:
            replay_path: Path to the scenario file to replay
            scenario_path: Path to write the new scenario log
        """
        self._stop_event.clear()
        commands = self._parse_log(replay_path)

        self._open_log(scenario_path)
        try:
            for action in commands:
                if self._stop_event.is_set():
                    logger.info("Replay interrupted")
                    break
                self._execute_and_log(action)
        finally:
            self._close_log()

    def _parse_log(self, path: str) -> List[FaultAction]:
        """Parse a scenario log file into a list of actions.

        Args:
            path: Path to the scenario log

        Returns:
            List of deserialized FaultAction instances
        """
        db = self.config.db_nodes
        extra = self.config.extra_nodes
        actions: List[FaultAction] = []

        with open(path, 'r') as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue

                # Strip optional timestamp prefix
                line = _TIMESTAMP_RE.sub('', line)
                if not line or line.startswith('#'):
                    continue

                # Split: first word is action name, rest is params
                parts = line.split(maxsplit=1)
                action_name = parts[0]
                params = parts[1] if len(parts) > 1 else ""

                cls = self.registry.get(action_name)
                if cls is None:
                    raise ValueError(
                        f"Line {line_no}: unknown action '{action_name}'. "
                        f"Available: {', '.join(self.registry.list_names())}"
                    )

                action = cls.deserialize(params, db, extra)
                actions.append(action)

        logger.info("Parsed scenario %s: %d actions", path, len(actions))
        return actions

    # ---- Control ----

    def stop(self) -> None:
        """Signal the engine to stop."""
        self._stop_event.set()

    def heal_all(self) -> None:
        """Heal all remaining network partitions (cleanup)."""
        Partitioners.heal_all(self.config.all_nodes)
