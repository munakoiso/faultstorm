"""
Fault actions for FaultStorm tests.

Each action knows how to:
  - execute() — perform the failure injection
  - heal() — reverse the failure (for healable actions like network partitions)
  - serialize() — convert its parameters to a log-file string
  - deserialize() — reconstruct from a log-file string (classmethod)

The FaultRegistry maps action names to their classes.
The engine writes lines in the format:

    [<timestamp>] +<action_name> <ordinal> <params>   (enable healable action)
    [<timestamp>] -<action_name> <ordinal>            (disable/heal action)
    [<timestamp>] <action_name> <ordinal> <params>    (fire-and-forget action)

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
from faultstorm.faults import partitioners

logger = logging.getLogger(__name__)


class FaultAction(ABC):
    """Abstract base class for fault actions.

    Every action receives db_nodes, extra_nodes, and an ordinal at
    construction time. The ordinal is a sequential number assigned by
    the engine and used by network partitions as the iptables chain ID.

    Subclasses must define a unique ``name`` class attribute.
    Healable actions (like network partitions) set ``healable = True``
    and override ``heal()`` to reverse the failure.
    """

    name: str = ""
    healable: bool = False

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0):
        self.db_nodes = db_nodes
        self.extra_nodes = extra_nodes
        self.ordinal = ordinal

    @property
    def all_nodes(self) -> List[str]:
        return self.db_nodes + self.extra_nodes

    @abstractmethod
    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        """Execute the action.

        Args:
            stop_event: Optional event to check for early termination.
        """

    def heal(self) -> None:
        """Reverse the failure. Override for healable actions.

        Default implementation is a no-op (for fire-and-forget actions).
        """

    @abstractmethod
    def serialize(self) -> str:
        """Serialize action parameters to a string.

        Must include ordinal as the first element.
        The engine will prepend the action name (and +/- prefix for
        healable actions).

        Returns:
            Parameter string starting with ordinal
        """

    @classmethod
    @abstractmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'FaultAction':
        """Reconstruct an action from a serialized parameter string.

        Must parse ordinal from the first element.

        Args:
            params: The parameter string (everything after the action name),
                    starts with ordinal
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
        action = cls.deserialize("3 postgres pg1", db_nodes, extra_nodes)

        # Get subset of classes (for random mode)
        classes = registry.get_classes(["kill", "partition_random_node"])
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
        if action_cls.name in self._registry:
            raise ValueError(f"name {action_cls.name} already registered")
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
    """Wait for a specified number of seconds (interruptible).

    Serialized format: ``<ordinal> <seconds>``
    """

    name = "wait"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0, seconds: int = 0):
        super().__init__(db_nodes, extra_nodes, ordinal)
        self.seconds = seconds

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if stop_event:
            stop_event.wait(self.seconds)
        else:
            time.sleep(self.seconds)

    def serialize(self) -> str:
        return f"{self.ordinal} {self.seconds}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'WaitAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        seconds = int(parts[1])
        return cls(db_nodes, extra_nodes, ordinal, seconds=seconds)


class KillProcessAction(FaultAction):
    """Kill a random process on a random DB node.

    Serialized format: ``<ordinal> <process> <node>``
    """

    name = "kill"

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 process: Optional[str] = None, node: Optional[str] = None,
                 processes: Optional[List[str]] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            ordinal: Sequential fault number (ignored by kill)
            process: Specific process to kill (None = pick random on execute)
            node: Specific node to target (None = pick random on execute)
            processes: Pool of process names to choose from.
                       Defaults to ["postgres", "pgconsul"].
        """
        super().__init__(db_nodes, extra_nodes, ordinal)
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
        return f"{self.ordinal} {self.process} {self.node}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'KillProcessAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        return cls(db_nodes, extra_nodes, ordinal, process=parts[1], node=parts[2])


class PartitionRandomHalvesAction(FaultAction):
    """Split all nodes into two random halves.

    Serialized format: ``<ordinal> <group1_csv> <group2_csv>``
    """

    name = "partition_random_halves"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 group1: Optional[List[str]] = None,
                 group2: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes, ordinal)
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

        chain_name = partitioners.get_chain_name(self.ordinal)

        group2_ips = [ClusterManager.get_node_ip(n) for n in self.group2]
        for node in self.group1:
            partitioners.create_chain(node, chain_name)
            partitioners.add_rule_to_chain(node, chain_name, group2_ips)

        group1_ips = [ClusterManager.get_node_ip(n) for n in self.group1]
        for node in self.group2:
            partitioners.create_chain(node, chain_name)
            partitioners.add_rule_to_chain(node, chain_name, group1_ips)

    def heal(self) -> None:
        logger.info("Healing partition halves ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self.all_nodes)

    def serialize(self) -> str:
        g1 = ','.join(self.group1 or [])
        g2 = ','.join(self.group2 or [])
        return f"{self.ordinal} {g1} {g2}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionRandomHalvesAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        group1 = parts[1].split(',')
        group2 = parts[2].split(',')
        return cls(db_nodes, extra_nodes, ordinal, group1=group1, group2=group2)


class PartitionMajoritiesRingAction(FaultAction):
    """Create a majorities-ring partition.

    Serialized format: ``<ordinal> <ordered_nodes_csv>``
    """

    name = "partition_majorities_ring"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 ordered: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes, ordinal)
        self.ordered = ordered

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.ordered is None:
            self.ordered = list(self.all_nodes)
            random.shuffle(self.ordered)
        logger.info("Partition ring: %s", self.ordered)

        n = len(self.ordered)
        majority = (n // 2) + 1
        chain_name = partitioners.get_chain_name(self.ordinal)

        for i, node in enumerate(self.ordered):
            visible = [self.ordered[(i + j) % n] for j in range(majority)]
            blocked = [nd for nd in self.ordered if nd not in visible]
            blocked_ips = [ClusterManager.get_node_ip(nd) for nd in blocked]
            partitioners.create_chain(node, chain_name)
            partitioners.add_rule_to_chain(node, chain_name, blocked_ips)

    def heal(self) -> None:
        logger.info("Healing partition ring ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self.all_nodes)

    def serialize(self) -> str:
        return f"{self.ordinal} {','.join(self.ordered or [])}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionMajoritiesRingAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        ordered = parts[1].split(',')
        return cls(db_nodes, extra_nodes, ordinal, ordered=ordered)


class PartitionRandomNodeAction(FaultAction):
    """Isolate a single random node from all others.

    Serialized format: ``<ordinal> <node>``
    """

    name = "partition_random_node"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 isolated: Optional[str] = None):
        super().__init__(db_nodes, extra_nodes, ordinal)
        self.isolated = isolated

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.isolated is None:
            self.isolated = random.choice(self.all_nodes)
        logger.info("Partition node: %s isolated", self.isolated)

        others = [n for n in self.all_nodes if n != self.isolated]
        chain_name = partitioners.get_chain_name(self.ordinal)

        blocked_ips = [ClusterManager.get_node_ip(n) for n in others]
        partitioners.create_chain(self.isolated, chain_name)
        partitioners.add_rule_to_chain(self.isolated, chain_name, blocked_ips)

        isolated_ip = ClusterManager.get_node_ip(self.isolated)
        for node in others:
            partitioners.create_chain(node, chain_name)
            partitioners.add_rule_to_chain(node, chain_name, [isolated_ip])

    def heal(self) -> None:
        logger.info("Healing partition node ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self.all_nodes)

    def serialize(self) -> str:
        return f"{self.ordinal} {self.isolated or ''}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str]) -> 'PartitionRandomNodeAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        isolated = parts[1] if len(parts) > 1 else None
        return cls(db_nodes, extra_nodes, ordinal, isolated=isolated)


# ---- Registry factory ----


def create_default_registry() -> FaultRegistry:
    """Create a registry with all built-in fault actions.

    Returns:
        FaultRegistry with built-in actions registered
    """
    registry = FaultRegistry()
    registry.register(WaitAction)
    registry.register(KillProcessAction)
    registry.register(PartitionRandomHalvesAction)
    registry.register(PartitionMajoritiesRingAction)
    registry.register(PartitionRandomNodeAction)
    return registry
