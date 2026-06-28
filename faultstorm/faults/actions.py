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
Class.deserialize(params_string, db_nodes, extra_nodes, load_node, dc_map).
"""

import logging
import random
import time
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type

from faultstorm.cluster import ClusterManager
from faultstorm.faults import partitioners

logger = logging.getLogger(__name__)


class FaultAction(ABC):
    """Abstract base class for fault actions.

    Every action receives db_nodes, extra_nodes, load_node, dc_map, and an
    ordinal at construction time. The ordinal is a sequential number assigned
    by the engine and used by network partitions as the iptables chain ID.

    ``load_node`` is the node running the load generator (write/read traffic).
    It is passed to all actions but currently only used by
    PartitionRandomSubnetAction. It is NOT serialized.

    ``dc_map`` maps datacenter names to lists of node names. It is passed to
    all actions but currently only used by PartitionRandomDcAction. It is NOT
    serialized.

    Subclasses must define a unique ``name`` class attribute.
    Healable actions (like network partitions) set ``healable = True``
    and override ``heal()`` to reverse the failure.
    """

    name: str = ""
    healable: bool = False

    #: Whether this action can target a specific host node via ``node=<name>``.
    #: Used by the engine to build complex (multi-fault) host scenarios.
    host_targetable: bool = False

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None):
        self.db_nodes = db_nodes
        self.extra_nodes = extra_nodes
        self.ordinal = ordinal
        self.load_node = load_node
        self.dc_map = dc_map or {}

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
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'FaultAction':
        """Reconstruct an action from a serialized parameter string.

        Must parse ordinal from the first element.

        Args:
            params: The parameter string (everything after the action name),
                    starts with ordinal
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            load_node: Load generator node name (not serialized)
            dc_map: DC-to-nodes mapping (not serialized)

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
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 seconds: int = 0):
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
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
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'WaitAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        seconds = int(parts[1])
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, seconds=seconds)


class KillProcessAction(FaultAction):
    """Kill a random process on a random DB node.

    Serialized format: ``<ordinal> <process> <node>``
    """

    name = "kill"
    host_targetable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 process: Optional[str] = None, node: Optional[str] = None,
                 processes: Optional[List[str]] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            ordinal: Sequential fault number (ignored by kill)
            load_node: Load generator node name (not used by kill)
            dc_map: DC-to-nodes mapping (not used by kill)
            process: Specific process to kill (None = pick random on execute)
            node: Specific node to target (None = pick random on execute)
            processes: Pool of process names to choose from.
                       Defaults to ["postgres", "pgconsul"].
        """
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
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
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'KillProcessAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, process=parts[1], node=parts[2])


class PartitionRandomHalvesAction(FaultAction):
    """Split all nodes into two random halves.

    Serialized format: ``<ordinal> <group1_csv> <group2_csv>``
    """

    name = "partition_random_halves"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 group1: Optional[List[str]] = None,
                 group2: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
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
            partitioners.add_drop_rules_by_src(node, chain_name, group2_ips)

        group1_ips = [ClusterManager.get_node_ip(n) for n in self.group1]
        for node in self.group2:
            partitioners.create_chain(node, chain_name)
            partitioners.add_drop_rules_by_src(node, chain_name, group1_ips)

    def heal(self) -> None:
        logger.info("Healing partition halves ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self.all_nodes)

    def serialize(self) -> str:
        g1 = ','.join(self.group1 or [])
        g2 = ','.join(self.group2 or [])
        return f"{self.ordinal} {g1} {g2}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'PartitionRandomHalvesAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        group1 = parts[1].split(',')
        group2 = parts[2].split(',')
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, group1=group1, group2=group2)


class PartitionMajoritiesRingAction(FaultAction):
    """Create a majorities-ring partition.

    Serialized format: ``<ordinal> <ordered_nodes_csv>``
    """

    name = "partition_majorities_ring"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 ordered: Optional[List[str]] = None):
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
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
            partitioners.add_drop_rules_by_src(node, chain_name, blocked_ips)

    def heal(self) -> None:
        logger.info("Healing partition ring ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self.all_nodes)

    def serialize(self) -> str:
        return f"{self.ordinal} {','.join(self.ordered or [])}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'PartitionMajoritiesRingAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        ordered = parts[1].split(',')
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, ordered=ordered)


class PartitionRandomNodeAction(FaultAction):
    """Isolate a single random node from all others.

    Serialized format: ``<ordinal> <node>``
    """

    name = "partition_random_node"
    healable = True
    host_targetable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 node: Optional[str] = None):
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
        self.node = node

    def _affected_nodes(self) -> List[str]:
        """All nodes affected by the partition (including load_node)."""
        nodes = list(self.all_nodes)
        if self.load_node and self.load_node not in nodes:
            nodes.append(self.load_node)
        return nodes

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.node is None:
            self.node = random.choice(self.all_nodes)
        logger.info("Partition node: %s isolated", self.node)

        affected = self._affected_nodes()
        others = [n for n in affected if n != self.node]
        chain_name = partitioners.get_chain_name(self.ordinal)

        blocked_ips = [ClusterManager.get_node_ip(n) for n in others]
        partitioners.create_chain(self.node, chain_name)
        partitioners.add_drop_rules_by_src(self.node, chain_name, blocked_ips)

        node_ip = ClusterManager.get_node_ip(self.node)
        for other in others:
            partitioners.create_chain(other, chain_name)
            partitioners.add_drop_rules_by_src(other, chain_name, [node_ip])

    def heal(self) -> None:
        logger.info("Healing partition node ordinal=%d", self.ordinal)
        partitioners.heal_partition(self.ordinal, self._affected_nodes())

    def serialize(self) -> str:
        return f"{self.ordinal} {self.node or ''}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'PartitionRandomNodeAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        node = parts[1] if len(parts) > 1 else None
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, node=node)


class PartitionRandomSubnetAction(FaultAction):
    """Apply a random directional network filter on a random DB node.

    Randomly chooses:
      - A DB node to apply iptables rules on
      - Traffic direction: ``input`` (incoming), ``output`` (outgoing),
        or ``both``
      - A subnet group to filter:
          1 (``zk``)  — ZooKeeper (extra) nodes
          2 (``db``)  — other DB nodes + load generator node
          3 (``all``) — both groups combined

    Only the chosen node gets iptables rules; other nodes are unaffected.
    This simulates a partial, asymmetric network failure.

    Serialized format:
        ``<ordinal> <node> <direction> <subnet_type> <blocked_nodes_csv>``
    """

    name = "partition_random_subnet"
    healable = True
    host_targetable = True

    # Direction constants
    DIRECTION_INPUT = "input"
    DIRECTION_OUTPUT = "output"
    DIRECTION_BOTH = "both"
    DIRECTIONS = [DIRECTION_INPUT, DIRECTION_OUTPUT, DIRECTION_BOTH]

    # Subnet type constants
    SUBNET_ZK = "zk"
    SUBNET_DB = "db"
    SUBNET_ALL = "all"
    SUBNET_TYPES = [SUBNET_ZK, SUBNET_DB, SUBNET_ALL]

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 node: Optional[str] = None,
                 direction: Optional[str] = None,
                 subnet_type: Optional[str] = None,
                 blocked_nodes: Optional[List[str]] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names (ZK nodes)
            ordinal: Sequential fault number (used as iptables chain ID)
            load_node: Load generator node name (included in ``db`` subnet)
            dc_map: DC-to-nodes mapping (not used by this action)
            node: DB node to apply rules on (None = pick random on execute)
            direction: Traffic direction to filter (None = pick random)
            subnet_type: Subnet group to block (None = pick random)
            blocked_nodes: Explicit list of blocked node names.
                           If None, computed from subnet_type on execute.
        """
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
        self.node = node
        self.direction = direction
        self.subnet_type = subnet_type
        self.blocked_nodes = blocked_nodes

    def _resolve_blocked_nodes(self) -> List[str]:
        """Compute the list of blocked nodes based on subnet_type."""
        zk_nodes = list(self.extra_nodes)
        other_nodes = [n for n in self.db_nodes if n != self.node]
        if self.load_node:
            other_nodes.append(self.load_node)
        if self.subnet_type == self.SUBNET_ZK:
            return zk_nodes
        elif self.subnet_type == self.SUBNET_DB:
            return other_nodes
        else:  # SUBNET_ALL
            return zk_nodes + other_nodes

    def _get_iptables_directions(self) -> List[str]:
        """Map direction string to iptables chain directions."""
        if self.direction == self.DIRECTION_INPUT:
            return ["INPUT"]
        elif self.direction == self.DIRECTION_OUTPUT:
            return ["OUTPUT"]
        else:  # DIRECTION_BOTH
            return ["INPUT", "OUTPUT"]

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if self.node is None:
            self.node = random.choice(self.db_nodes)
        if self.direction is None:
            self.direction = random.choice(self.DIRECTIONS)
        if self.subnet_type is None:
            self.subnet_type = random.choice(self.SUBNET_TYPES)
        if self.blocked_nodes is None:
            self.blocked_nodes = self._resolve_blocked_nodes()

        blocked_ips = [ClusterManager.get_node_ip(n) for n in self.blocked_nodes]
        ipt_directions = self._get_iptables_directions()

        logger.info(
            "Partition random subnet: node=%s direction=%s subnet=%s "
            "blocked=%s",
            self.node, self.direction, self.subnet_type, self.blocked_nodes,
        )

        for ipt_dir in ipt_directions:
            chain_name = partitioners.get_directional_chain_name(
                self.ordinal, ipt_dir
            )
            partitioners.create_chain_for_direction(
                self.node, chain_name, ipt_dir
            )
            if ipt_dir == "INPUT":
                partitioners.add_drop_rules_by_src(
                    self.node, chain_name, blocked_ips
                )
            else:  # OUTPUT
                partitioners.add_drop_rules_by_dest(
                    self.node, chain_name, blocked_ips
                )

    def heal(self) -> None:
        logger.info(
            "Healing partition random subnet ordinal=%d node=%s",
            self.ordinal, self.node,
        )
        ipt_directions = self._get_iptables_directions()
        partitioners.heal_directional_partition(
            self.ordinal, self.node, ipt_directions
        )

    def serialize(self) -> str:
        blocked_csv = ','.join(self.blocked_nodes or [])
        return (f"{self.ordinal} {self.node} {self.direction} "
                f"{self.subnet_type} {blocked_csv}")

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'PartitionRandomSubnetAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        node = parts[1]
        direction = parts[2]
        subnet_type = parts[3]
        blocked_nodes = parts[4].split(',') if len(parts) > 4 and parts[4] else []
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, node=node, direction=direction,
                   subnet_type=subnet_type, blocked_nodes=blocked_nodes)


class PartitionRandomDcAction(FaultAction):
    """Isolate all nodes of a random datacenter from all other nodes.

    Similar to PartitionRandomNodeAction, but instead of isolating a single
    node, it isolates all nodes belonging to a randomly chosen DC.

    Requires ``dc_map`` to be populated (mapping DC names → node lists).
    The list of nodes for a DC is resolved from ``dc_map`` at execution time.

    Serialized format: ``<ordinal> <dc_name>``
    """

    name = "partition_random_dc"
    healable = True

    def __init__(self, db_nodes: List[str], extra_nodes: List[str],
                 ordinal: int = 0,
                 load_node: Optional[str] = None,
                 dc_map: Optional[Dict[str, List[str]]] = None,
                 dc_name: Optional[str] = None):
        """Initialize.

        Args:
            db_nodes: Database node names
            extra_nodes: Extra infrastructure node names
            ordinal: Sequential fault number (used as iptables chain ID)
            load_node: Load generator node name (not used by this action)
            dc_map: DC-to-nodes mapping (used to pick a random DC
                    and resolve its nodes)
            dc_name: Specific DC to isolate (None = pick random on execute)
        """
        super().__init__(db_nodes, extra_nodes, ordinal, load_node=load_node,
                         dc_map=dc_map)
        self.dc_name = dc_name

    def _get_dc_nodes(self) -> List[str]:
        """Get list of nodes for the chosen DC from dc_map."""
        return list(self.dc_map.get(self.dc_name, []))

    def _affected_nodes(self) -> List[str]:
        """All nodes affected by the partition (including load_node)."""
        nodes = list(self.all_nodes)
        if self.load_node and self.load_node not in nodes:
            nodes.append(self.load_node)
        return nodes

    def execute(self, stop_event: Optional[threading.Event] = None) -> None:
        if not self.dc_map:
            logger.warning("partition_random_dc: dc_map is empty, skipping")
            return

        if self.dc_name is None:
            self.dc_name = random.choice(list(self.dc_map.keys()))

        dc_nodes = self._get_dc_nodes()
        if not dc_nodes:
            logger.warning("partition_random_dc: DC %s has no nodes, skipping",
                           self.dc_name)
            return

        affected = self._affected_nodes()
        others = [n for n in affected if n not in dc_nodes]
        logger.info("Partition DC %s: isolated=%s others=%s",
                    self.dc_name, dc_nodes, others)

        chain_name = partitioners.get_chain_name(self.ordinal)

        # Block traffic from others on each DC node
        others_ips = [ClusterManager.get_node_ip(n) for n in others]
        for node in dc_nodes:
            partitioners.create_chain(node, chain_name)
            partitioners.add_drop_rules_by_src(node, chain_name, others_ips)

        # Block traffic from DC nodes on each other node
        dc_ips = [ClusterManager.get_node_ip(n) for n in dc_nodes]
        for node in others:
            partitioners.create_chain(node, chain_name)
            partitioners.add_drop_rules_by_src(node, chain_name, dc_ips)

    def heal(self) -> None:
        logger.info("Healing partition DC %s ordinal=%d",
                    self.dc_name, self.ordinal)
        partitioners.heal_partition(self.ordinal, self._affected_nodes())

    def serialize(self) -> str:
        return f"{self.ordinal} {self.dc_name}"

    @classmethod
    def deserialize(cls, params: str, db_nodes: List[str],
                    extra_nodes: List[str],
                    load_node: Optional[str] = None,
                    dc_map: Optional[Dict[str, List[str]]] = None) -> 'PartitionRandomDcAction':
        parts = params.strip().split()
        ordinal = int(parts[0])
        dc_name = parts[1] if len(parts) > 1 else None
        return cls(db_nodes, extra_nodes, ordinal, load_node=load_node,
                   dc_map=dc_map, dc_name=dc_name)


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
    registry.register(PartitionRandomSubnetAction)
    registry.register(PartitionRandomDcAction)
    return registry
