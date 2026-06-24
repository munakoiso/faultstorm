"""
Fault actions for FaultStorm tests.

Each action knows how to:
  - execute() — perform the failure injection
  - serialize() — convert its parameters to a log-file string
  - deserialize() — reconstruct from a log-file string (classmethod)

The FaultRegistry maps action names to their classes.
The engine writes lines in the format:

    [<timestamp>] <action_name> <serialized_params>

and during replay, looks up the class by action_name and calls
Class.deserialize(params_string, db_nodes, extra_nodes).
"""

import logging
import random
import time
import threading
from abc import ABC, abstractmethod
from typing import List, Optional, Type

from faultstorm.cluster import ClusterManager
from faultstorm.faults.partitioners import Partitioners

logger = logging.getLogger(__name__)


class FaultAction(ABC):
    """Abstract base class for fault actions.

    Every action receives db_nodes and extra_nodes at construction time.
    Subclasses must define a unique ``name`` class attribute.

    Lifecycle:
      - Random mode: engine creates an instance, calls execute(), then
        serialize(), and writes ``name + " " + serialized`` to the log.
      - Replay mode: engine reads a line, splits ``name`` from the rest,
        looks up the class in the registry, calls Class.deserialize(rest, ...),
        then execute() on the result.
    """

    name: str = ""

    def __init__(self, db_nodes: List[str], extra_nodes: List[str]):
        self.db_nodes = db_nodes
        self.extra_nodes = extra_nodes

    @property
    def all_nodes(self) -> List[str]:
        return self.db_nodes + self.extra_nodes

    @abstractmethod
    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        """Execute the action.

        Args:
            stop_event: Optional event to check for early termination.
        """

    @abstractmethod
    def serialize(self) -> str:
        """Serialize action parameters to a string.

        The engine will prepend the action name, so this should contain
        only the parameters.

        Returns:
            Parameter string (may be empty)
        """

    @classmethod
    @abstractmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'FaultAction':
        """Reconstruct an action from a serialized parameter string.

        Args:
            params: The parameter string (everything after the action name)
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names

        Returns:
            A fully configured FaultAction ready to execute()
        """


class FaultRegistry:
    """Registry mapping action names to their classes.

    Usage::

        registry = FaultRegistry()
        registry.register(KillProcessAction)
        registry.register(PartitionRandomHalvesAction)
        registry.register(RestartMySQLAction)  # custom

        # Get class by name (for deserialization)
        cls = registry.get("kill")
        action = cls.deserialize("postgres pg1", db_nodes, extra_nodes)

        # Get subset of classes (for random mode)
        classes = registry.get_classes(["kill", "switchover"])
    """

    def __init__(self) -> None:
        self._registry: dict[str, Type[FaultAction]] = {}

    def register(self, action_cls: Type[FaultAction]) -> 'FaultRegistry':
        """Register an action class.

        Args:
            action_cls: FaultAction subclass (the class itself, not an instance)

        Returns:
            self (for chaining)
        """
        if not action_cls.name:
            raise ValueError(f"{action_cls.__name__} must have a non-empty 'name'")
        self._registry[action_cls.name] = action_cls
        return self

    def get(self, name: str) -> Optional[Type[FaultAction]]:
        """Get action class by name.

        Args:
            name: Action name

        Returns:
            Action class or None
        """
        return self._registry.get(name)

    def get_classes(self, names: Optional[List[str]] = None) -> List[Type[FaultAction]]:
        """Get a list of action classes, optionally filtered by name.

        Args:
            names: If set, return only classes with these names.
                   If None, return all registered classes.

        Returns:
            List of FaultAction subclasses

        Raises:
            ValueError: If a requested name is not registered
        """
        if names is None:
            return list(self._registry.values())
        result = []
        for name in names:
            cls = self._registry.get(name)
            if cls is None:
                available = ', '.join(sorted(self._registry.keys()))
                raise ValueError(
                    f"Unknown fault action '{name}'. Available: {available}"
                )
            result.append(cls)
        return result

    def list_names(self) -> List[str]:
        """List all registered action names.

        Returns:
            Sorted list of action names
        """
        return sorted(self._registry.keys())


# ---- Built-in actions ----


class WaitAction(FaultAction):
    """Wait for a specified number of seconds (interruptible)."""

    name = "wait"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 seconds: int = 0):
        super().__init__(db_nodes, extra_nodes)
        self.seconds = seconds

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if stop_event:
            stop_event.wait(self.seconds)
        else:
            time.sleep(self.seconds)

    def serialize(self) -> str:
        return str(self.seconds)

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'WaitAction':
        return cls(db_nodes, extra_nodes, seconds=int(params.strip()))


class HealAllAction(FaultAction):
    """Remove all network partitions."""

    name = "heal_all"

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        Partitioners.heal_all(self.all_nodes)

    def serialize(self) -> str:
        return ""

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'HealAllAction':
        return cls(db_nodes, extra_nodes)


class KillProcessAction(FaultAction):
    """Kill a random process on a random DB node.

    Serialized format: ``<process> <node>``
    """

    name = "kill"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 process: Optional[str] = None, node: Optional[str] = None,
                 processes: Optional[List[str]] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            process: Specific process to kill (None = pick random on execute)
            node: Specific node to target (None = pick random on execute)
            processes: Pool of process names to choose from.
                       Defaults to ["postgres", "pgconsul"].
        """
        super().__init__(db_nodes, extra_nodes)
        self.process = process
        self.node = node
        self.processes = processes or ["postgres", "pgconsul"]

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.process is None:
            self.process = random.choice(self.processes)
        if self.node is None:
            self.node = random.choice(self.db_nodes)
        logger.info("Killing %s on %s", self.process, self.node)
        try:
            ClusterManager.exec_on_node(
                self.node, ["pkill", "-9", self.process], timeout=10
            )
        except Exception as e:
            logger.warning("Kill %s on %s failed: %s", self.process, self.node, e)

    def serialize(self) -> str:
        return f"{self.process} {self.node}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'KillProcessAction':
        parts = params.strip().split()
        return cls(db_nodes, extra_nodes, process=parts[0], node=parts[1])


class SwitchoverAction(FaultAction):
    """Execute switchover on a random DB node.

    Serialized format: ``<node>``
    """

    name = "switchover"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 node: Optional[str] = None,
                 command: Optional[List[str]] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            node: Specific node (None = pick random on execute)
            command: Custom switchover command.
                     Defaults to ["timeout", "10", "pgconsul-util", "switchover", "-y"].
        """
        super().__init__(db_nodes, extra_nodes)
        self.node = node
        self.command = command or ["timeout", "10", "pgconsul-util", "switchover", "-y"]

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.node is None:
            self.node = random.choice(self.db_nodes)
        logger.info("Switchover on %s", self.node)
        try:
            ClusterManager.exec_on_node(self.node, self.command, timeout=15)
        except Exception as e:
            logger.warning("Switchover on %s failed: %s", self.node, e)

    def serialize(self) -> str:
        return self.node or ""

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'SwitchoverAction':
        return cls(db_nodes, extra_nodes, node=params.strip())


class PartitionRandomHalvesAction(FaultAction):
    """Split all nodes into two random halves.

    Serialized format: ``<group1_csv> <group2_csv>``
    """

    name = "partition_random_halves"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 group1: Optional[List[str]] = None,
                 group2: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes)
        self.group1 = group1
        self.group2 = group2

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.group1 is None or self.group2 is None:
            nodes = list(self.all_nodes)
            random.shuffle(nodes)
            mid = len(nodes) // 2
            self.group1 = nodes[:mid]
            self.group2 = nodes[mid:]
        logger.info("Partition halves: %s | %s", self.group1, self.group2)
        Partitioners.partition_halves(self.group1, self.group2)

    def serialize(self) -> str:
        g1 = ','.join(self.group1 or [])
        g2 = ','.join(self.group2 or [])
        return f"{g1} {g2}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionRandomHalvesAction':
        parts = params.strip().split()
        group1 = parts[0].split(',')
        group2 = parts[1].split(',')
        return cls(db_nodes, extra_nodes, group1=group1, group2=group2)


class PartitionMajoritiesRingAction(FaultAction):
    """Create a majorities-ring partition.

    Serialized format: ``<ordered_nodes_csv>``
    """

    name = "partition_majorities_ring"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordered: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes)
        self.ordered = ordered

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.ordered is None:
            self.ordered = list(self.all_nodes)
            random.shuffle(self.ordered)
        logger.info("Partition ring: %s", self.ordered)
        Partitioners.partition_ring(self.ordered)

    def serialize(self) -> str:
        return ','.join(self.ordered or [])

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionMajoritiesRingAction':
        ordered = params.strip().split(',')
        return cls(db_nodes, extra_nodes, ordered=ordered)


class PartitionRandomNodeAction(FaultAction):
    """Isolate a single random node from all others.

    Serialized format: ``<node>``
    """

    name = "partition_random_node"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 isolated: Optional[str] = None):
        super().__init__(db_nodes, extra_nodes)
        self.isolated = isolated

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.isolated is None:
            self.isolated = random.choice(self.all_nodes)
        logger.info("Partition node: %s isolated", self.isolated)
        Partitioners.partition_isolated_node(self.isolated, self.all_nodes)

    def serialize(self) -> str:
        return self.isolated or ""

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionRandomNodeAction':
        return cls(db_nodes, extra_nodes, isolated=params.strip())


# ---- Registry factory ----


def create_default_registry() -> FaultRegistry:
    """Create a registry with all built-in fault actions.

    Returns:
        FaultRegistry with built-in actions registered
    """
    registry = FaultRegistry()
    registry.register(WaitAction)
    registry.register(HealAllAction)
    registry.register(KillProcessAction)
    registry.register(SwitchoverAction)
    registry.register(PartitionRandomHalvesAction)
    registry.register(PartitionMajoritiesRingAction)
    registry.register(PartitionRandomNodeAction)
    return registry
