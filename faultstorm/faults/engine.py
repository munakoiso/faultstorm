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

        Each cycle injects 2 faults via ``_inject_complex_fault``, waits
        for ``fault_active_duration``, heals all active faults in random
        order with random waits, then pauses for ``fault_pause_duration``.

        When ``complex_faults_enabled`` is True and there are eligible
        host-targetable fault types, each injection randomly chooses
        between a single regular fault and a multi-fault combo (1–3
        host-targetable faults on one DB node with random wait
        intervals).

        When ``complex_faults_enabled`` is False (or no host-targetable
        types are available), each injection is always a single regular
        fault.

        Args:
            duration: Total duration in seconds
            scenario_path: Path to write the scenario log
        """
        self._stop_event.clear()
        fault_classes = self.registry.get_classes(self.config.fault_types)

        # Build list of host-targetable classes for complex faults
        complex_classes: List[type] = []
        if self.config.complex_faults_enabled:
            complex_classes = [c for c in fault_classes if c.host_targetable]
            if complex_classes:
                logger.info(
                    "Complex faults enabled with %d eligible types: %s",
                    len(complex_classes),
                    [c.name for c in complex_classes],
                )
            else:
                logger.info(
                    "Complex faults enabled but no host-targetable types "
                    "found in fault_types; using single-fault mode"
                )

        self._open_log(scenario_path)
        try:
            self._random_loop(duration, fault_classes, complex_classes)
        finally:
            self._close_log()

    def _random_loop(self, duration: int,
                     fault_classes: List[type],
                     complex_classes: List[type]) -> None:
        """Main random-mode loop.

        Runs fault injection cycles for at most ``duration`` seconds.
        A background timer sets ``_stop_event`` when time is up, which
        immediately interrupts any running ``WaitAction`` and causes
        the loop to exit.  The timer is cancelled if the loop is
        stopped earlier by an external ``stop()`` call.
        """
        timer = threading.Timer(duration, self._stop_event.set)
        timer.daemon = True
        timer.start()
        logger.info("Fault engine timer started: %d seconds", duration)

        try:
            self._do_random_loop(fault_classes, complex_classes)
        finally:
            timer.cancel()

    def _do_random_loop(self, fault_classes: List[type],
                        complex_classes: List[type]) -> None:
        """Inner random-mode loop (runs until ``_stop_event`` is set)."""
        db = self.config.db_nodes
        extra = self.config.extra_nodes
        load_node = self.config.load_node
        dc_map = self.dc_map
        min_wait = self.config.complex_fault_min_wait
        max_wait = self.config.complex_fault_max_wait

        while not self._stop_event.is_set():
            # 2 complex fault injections per cycle
            for _ in range(2):
                self._inject_complex_fault(db, extra, load_node, dc_map,
                                           fault_classes, complex_classes)
                wait_sec = random.randint(min_wait, max_wait)
                wait_a_bit = WaitAction(db, extra, self._get_next_ordinal(),
                                        load_node=load_node, dc_map=dc_map,
                                        seconds=wait_sec)
                self._execute_and_log(wait_a_bit)

            # Wait (active phase)
            wait_active = WaitAction(db, extra, self._get_next_ordinal(),
                                     load_node=load_node, dc_map=dc_map,
                                     seconds=self.config.fault_active_duration)
            self._execute_and_log(wait_active)
            if self._stop_event.is_set():
                break

            # Heal all active faults (random order, random waits)
            self._heal_all_active()

            # Wait (pause phase)
            wait_pause = WaitAction(db, extra, self._get_next_ordinal(),
                                    load_node=load_node, dc_map=dc_map,
                                    seconds=self.config.fault_pause_duration)
            self._execute_and_log(wait_pause)

    def _inject_complex_fault(self, db: List[str], extra: List[str],
                              load_node: Optional[str],
                              dc_map: Dict[str, List[str]],
                              fault_classes: List[type],
                              complex_classes: List[type]) -> None:
        """Inject a single fault or a multi-fault combo on one host.

        If ``complex_classes`` is non-empty, randomly chooses between:
          - A single fault from all enabled types (50% chance)
          - A combo of 1–3 host-targetable faults on one random DB
            node with random wait intervals between them (50% chance)

        If ``complex_classes`` is empty, always injects a single fault
        from all enabled types.

        Args:
            db: Database node names
            extra: Extra infrastructure node names
            load_node: Load generator node name
            dc_map: DC-to-nodes mapping
            fault_classes: All enabled fault action classes
            complex_classes: Host-targetable subset (may be empty)
        """
        min_wait = self.config.complex_fault_min_wait
        max_wait = self.config.complex_fault_max_wait

        use_combo = complex_classes and random.random() < 0.5

        if not use_combo:
            # Single regular fault
            cls = random.choice(fault_classes)
            ordinal = self._get_next_ordinal()
            action = cls(db, extra, ordinal, load_node=load_node,
                         dc_map=dc_map)
            self._execute_and_log(action)
            if action.healable:
                self._active_faults.append(action)
        else:
            # Multi-fault combo on one host
            target_node = random.choice(db)
            fault_count = random.randint(1, 3)
            logger.info("Complex fault: %d faults on %s",
                        fault_count, target_node)

            for i in range(fault_count):
                cls = random.choice(complex_classes)
                ordinal = self._get_next_ordinal()
                action = cls(db, extra, ordinal, load_node=load_node,
                             dc_map=dc_map, node=target_node)
                self._execute_and_log(action)
                if action.healable:
                    self._active_faults.append(action)

                # Random wait between component faults (not after the last)
                if i < fault_count - 1:
                    wait_sec = random.randint(min_wait, max_wait)
                    wait = WaitAction(db, extra, self._get_next_ordinal(),
                                      load_node=load_node, dc_map=dc_map,
                                      seconds=wait_sec)
                    self._execute_and_log(wait)

    def _execute_and_log(self, action: FaultAction) -> None:
        """Execute an action and write it to the scenario log."""
        try:
            action.execute(self._stop_event)
        except Exception as e:
            logger.error("Action %s failed: %s", action.name, e)
        self._write_action(action, healing=False)

    def _heal_all_active(self) -> None:
        """Heal all currently active faults in random order with random waits."""
        faults = list(self._active_faults)
        random.shuffle(faults)

        min_wait = self.config.complex_fault_min_wait
        max_wait = self.config.complex_fault_max_wait

        for i, action in enumerate(faults):
            try:
                action.heal()
            except Exception as e:
                logger.error("Heal %s ordinal=%d failed: %s",
                             action.name, action.ordinal, e)
            self._write_action(action, healing=True)

            # Random wait between heals (not after the last)
            if i < len(faults) - 1:
                wait_sec = random.randint(min_wait, max_wait)
                wait = WaitAction(
                    self.config.db_nodes, self.config.extra_nodes,
                    self._get_next_ordinal(),
                    load_node=self.config.load_node, dc_map=self.dc_map,
                    seconds=wait_sec,
                )
                self._execute_and_log(wait)

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
