"""
Fault engine for FaultStorm tests.

Two modes:
  - Random: picks random actions from a configured subset, executes them,
    waits, then heals all active faults, waits again, and repeats.
    Every action is written to a scenario log file.
  - Replay: reads a previously written scenario log, deserializes each line
    back into an action, and executes them in order.

The engine maintains a sequential ordinal counter shared across all faults.
Network partition actions use this ordinal as the iptables chain ID.

Scenario log format (line-based)::

    # FaultStorm scenario log
    # Generated at 2026-06-23T14:00:00.000

    [2026-06-23T14:00:10.123] kill 1 postgres postgresql1
    [2026-06-23T14:00:10.456] +partition_random_node 2 zookeeper2
    [2026-06-23T14:00:10.789] wait 3 60
    [2026-06-23T14:00:70.012] -partition_random_node 2 zookeeper2
    [2026-06-23T14:01:10.345] wait 4 60
    ...

Lines prefixed with ``+`` indicate enabling a healable fault.
Lines prefixed with ``-`` indicate healing/disabling a fault.
Lines without a prefix are fire-and-forget actions.
"""

import logging
import random
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, IO

from faultstorm.config import TestConfig
from faultstorm.faults.actions import (
    FaultAction,
    FaultRegistry,
    WaitAction,
)

logger = logging.getLogger(__name__)

# Strip optional timestamp prefix: [2026-06-23T14:05:30.123]
_TIMESTAMP_RE = re.compile(r'^\[[\d\-T:.]+\]\s*')


def _timestamp() -> str:
    """Current timestamp for scenario lines."""
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]


class FaultEngine:
    """Engine for injecting failures into the cluster.

    Maintains a list of active (healable) faults and a sequential ordinal
    counter. Supports two modes:
      - run_random(): random faults with heal cycles
      - run_replay(): deterministic replay from a scenario log
    """

    def __init__(self, config: TestConfig, registry: FaultRegistry,
                 dc_map: Optional[Dict[str, List[str]]] = None):
        """Initialize fault engine.

        Args:
            config: Test configuration
            registry: Registry with all known action classes
            dc_map: Datacenter mapping (DC name → list of node names).
                    Passed to fault actions; not serialized.
        """
        self.config = config
        self.registry = registry
        self.dc_map = dc_map or {}
        self._stop_event = threading.Event()
        self._log_file: Optional[IO] = None
        self._active_faults: List[FaultAction] = []
        self._next_ordinal = 0

    def _get_next_ordinal(self) -> int:
        """Get next sequential ordinal number."""
        self._next_ordinal += 1
        return self._next_ordinal

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

    def _write_action(self, action: FaultAction, healing: bool = False) -> None:
        """Write a single action to the scenario log.

        Format for healable actions:
            [timestamp] +action_name params   (enable)
            [timestamp] -action_name params   (heal)

        Format for fire-and-forget actions:
            [timestamp] action_name params

        Args:
            action: Executed action to record
            healing: True if this is a heal event for a healable action
        """
        if self._log_file is None:
            return
        params = action.serialize()
        if action.healable:
            prefix = "-" if healing else "+"
        else:
            prefix = ""
        name = f"{prefix}{action.name}"
        if params:
            line = f"[{_timestamp()}] {name} {params}\n"
        else:
            line = f"[{_timestamp()}] {name}\n"
        self._log_file.write(line)
        self._log_file.flush()

    # ---- Random mode ----

    def run_random(self, duration: int, scenario_path: str) -> None:
        """Run random fault cycles for the specified duration.

        Strategy: pick 2 random fault actions → execute them → wait
        (fault_active_duration) → heal all active faults → wait
        (fault_pause_duration) → repeat.

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
        load_node = self.config.load_node
        dc_map = self.dc_map

        while not self._stop_event.is_set():
            # 2 random faults
            for _ in range(2):
                cls = random.choice(fault_classes)
                ordinal = self._get_next_ordinal()
                action = cls(db, extra, ordinal, load_node=load_node,
                             dc_map=dc_map)
                self._execute_and_log(action)
                if action.healable:
                    self._active_faults.append(action)
                wait_a_bit = WaitAction(db, extra, self._get_next_ordinal(),
                                     load_node=load_node, dc_map=dc_map,
                                     seconds=15)
                self._execute_and_log(wait_a_bit)


            # Wait (active phase)
            wait_active = WaitAction(db, extra, self._get_next_ordinal(),
                                     load_node=load_node, dc_map=dc_map,
                                     seconds=self.config.fault_active_duration)
            self._execute_and_log(wait_active)
            if self._stop_event.is_set():
                break

            # Heal all active faults
            self._heal_all_active()

            # Wait (pause phase)
            wait_pause = WaitAction(db, extra, self._get_next_ordinal(),
                                    load_node=load_node, dc_map=dc_map,
                                    seconds=self.config.fault_pause_duration)
            self._execute_and_log(wait_pause)

    def _execute_and_log(self, action: FaultAction) -> None:
        """Execute an action and write it to the scenario log."""
        try:
            action.execute(self._stop_event)
        except Exception as e:
            logger.error("Action %s failed: %s", action.name, e)
        self._write_action(action, healing=False)

    def _heal_all_active(self) -> None:
        """Heal all currently active faults and log each heal event."""
        for action in self._active_faults:
            try:
                action.heal()
            except Exception as e:
                logger.error("Heal %s ordinal=%d failed: %s",
                             action.name, action.ordinal, e)
            self._write_action(action, healing=True)
        self._active_faults.clear()

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
            for action, is_heal in commands:
                if self._stop_event.is_set():
                    logger.info("Replay interrupted")
                    break
                if is_heal:
                    try:
                        action.heal()
                    except Exception as e:
                        logger.error("Heal %s ordinal=%d failed: %s",
                                     action.name, action.ordinal, e)
                    self._write_action(action, healing=True)
                else:
                    self._execute_and_log(action)
        finally:
            self._close_log()

    def _parse_log(self, path: str) -> List[tuple]:
        """Parse a scenario log file into a list of (action, is_heal) tuples.

        Args:
            path: Path to the scenario log

        Returns:
            List of (FaultAction, bool) tuples. is_heal=True means heal event.
        """
        db = self.config.db_nodes
        extra = self.config.extra_nodes
        load_node = self.config.load_node
        dc_map = self.dc_map
        results: List[tuple] = []

        with open(path, 'r') as f:
            for line_no, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue

                # Strip optional timestamp prefix
                line = _TIMESTAMP_RE.sub('', line)
                if not line or line.startswith('#'):
                    continue

                # Check for +/- prefix (healable actions)
                is_heal = False
                if line.startswith('+') or line.startswith('-'):
                    is_heal = line.startswith('-')
                    line = line[1:]

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

                action = cls.deserialize(params, db, extra, load_node=load_node,
                                         dc_map=dc_map)
                results.append((action, is_heal))

        logger.info("Parsed scenario %s: %d entries", path, len(results))
        return results

    # ---- Control ----

    def stop(self) -> None:
        """Signal the engine to stop."""
        self._stop_event.set()

    def heal_all(self) -> None:
        """Heal all remaining active faults (cleanup)."""
        self._heal_all_active()
