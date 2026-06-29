"""
Configuration for FaultStorm tests.

Database-agnostic: node lists, fault types, and timing are configurable.
Database-specific settings (connection parameters, etc.) should be handled
by the DatabaseClient implementation.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TestConfig:
    """Configuration for a single fault-injection test.

    Attributes:
        name: Test name for logging
        db_nodes: List of database node names (used for fault targeting)
        extra_nodes: List of additional infrastructure nodes (e.g. ZooKeeper)
        write_phase_duration: How long to write (seconds)
        read_phase_duration: How long to read for validation (seconds)
        add_interval: Delay between writes (seconds)
        read_interval: Delay between reads (seconds)
        fault_active_duration: How long faults stay active per cycle (seconds)
        fault_pause_duration: How long to pause between fault cycles (seconds)
        fault_types: List of fault action names to use
        operations_log: Path to write operations log (JSON)
        scenario_log: Path to write scenario log
        replay_scenario: If set, replay faults from this scenario file
    """

    name: str = "default"

    # Cluster nodes
    db_nodes: List[str] = field(default_factory=list)
    extra_nodes: List[str] = field(default_factory=list)

    # Test duration (seconds)
    write_phase_duration: int = 7200
    read_phase_duration: int = 600

    # Operation intervals (seconds)
    add_interval: float = 0.02
    read_interval: float = 1.0

    # Application-side timeout for each DB operation (seconds).
    # If a single add/read does not complete within this time, the
    # operation is treated as indeterminate.
    operation_timeout: float = 5.0

    # Number of parallel writer threads per DB node.
    writers_per_node: int = 2

    # Fault configuration
    fault_active_duration: int = 60
    fault_pause_duration: int = 60
    fault_types: List[str] = field(default_factory=lambda: [
        "partition_random_halves",
        "partition_majorities_ring",
        "partition_random_node",
        "partition_random_subnet",
        "partition_random_dc",
        "kill",
    ])

    # Complex faults: combine 1-3 host-targetable faults on a single DB node.
    # The list of eligible fault types is built dynamically from ``fault_types``
    # by selecting those with ``host_targetable = True``.
    complex_faults_enabled: bool = True
    complex_fault_min_wait: int = 0
    complex_fault_max_wait: int = 20

    parallel_faults_count: int = 2

    # Maximum number of destructive actions allowed per test run.
    # Actions with ``destructive = True`` are counted; once the limit
    # is reached the engine skips further destructive actions.
    # None means unlimited.
    max_destructive_actions: Optional[int] = 1

    # Logging
    operations_log: str = "logs/operations.log"
    scenario_log: str = "logs/scenario.log"

    # Replay mode
    replay_scenario: Optional[str] = None

    # Load generator node (the node running write/read traffic).
    # Passed to fault actions but only used by PartitionRandomSubnetAction.
    load_node: Optional[str] = None

    # Per-action constructor overrides.
    # Maps action name to a dict of keyword arguments that will be
    # merged into every constructor call for that action type.
    # Example: ``{"freeze_processes": {"processes": ["postgres", "pgconsul"]}}``
    action_params: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def all_nodes(self) -> List[str]:
        """All cluster nodes (db + extra)."""
        return self.db_nodes + self.extra_nodes
