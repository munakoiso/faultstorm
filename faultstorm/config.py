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

    # Fault configuration
    fault_active_duration: int = 60
    fault_pause_duration: int = 60
    fault_types: List[str] = field(default_factory=lambda: [
        "partition_random_halves",
        "partition_majorities_ring",
        "partition_random_node",
        "kill",
        "switchover",
    ])

    # Logging
    operations_log: str = "logs/operations.log"
    scenario_log: str = "logs/scenario.log"

    # Replay mode
    replay_scenario: Optional[str] = None

    @property
    def all_nodes(self) -> List[str]:
        """All cluster nodes (db + extra)."""
        return self.db_nodes + self.extra_nodes


# ---- pgconsul-specific presets ----

def get_pgconsul_config(name: str = "default", **overrides: Any) -> TestConfig:
    """Get a TestConfig preset for pgconsul clusters.

    Args:
        name: Configuration name
        **overrides: Override any TestConfig field

    Returns:
        TestConfig configured for pgconsul
    """
    defaults: Dict[str, Any] = dict(
        name=name,
        db_nodes=["postgresql1", "postgresql2", "postgresql3"],
        extra_nodes=["zookeeper1", "zookeeper2", "zookeeper3"],
    )
    defaults.update(overrides)
    return TestConfig(**defaults)


def get_default_config() -> TestConfig:
    """Get default pgconsul test configuration."""
    return get_pgconsul_config("default")


def get_quick_config() -> TestConfig:
    """Get quick pgconsul test configuration for fast testing."""
    return get_pgconsul_config(
        "quick",
        write_phase_duration=120,
        read_phase_duration=60,
        add_interval=0.1,
    )


def get_intensive_config() -> TestConfig:
    """Get intensive pgconsul test configuration with more faults."""
    return get_pgconsul_config(
        "intensive",
        write_phase_duration=3600,
        fault_active_duration=30,
        fault_pause_duration=30,
    )
