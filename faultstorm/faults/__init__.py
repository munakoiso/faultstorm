"""
Faults package for FaultStorm.

Provides fault injection actions, a registry for custom actions,
and an engine with random/replay modes.
"""

from faultstorm.faults.actions import (
    FaultAction,
    FaultRegistry,
    FreezeProcessesAction,
    FreezeProcessesGroupAction,
    KillProcessAction,
    PartitionMajoritiesRingAction,
    PartitionRandomDcAction,
    PartitionRandomHalvesAction,
    PartitionRandomNodeAction,
    PartitionRandomSubnetAction,
    WaitAction,
    create_default_registry,
)
from faultstorm.faults.engine import FaultEngine

__all__ = [
    "FaultAction",
    "FaultRegistry",
    "WaitAction",
    "KillProcessAction",
    "PartitionRandomHalvesAction",
    "PartitionMajoritiesRingAction",
    "PartitionRandomNodeAction",
    "PartitionRandomSubnetAction",
    "PartitionRandomDcAction",
    "FreezeProcessesAction",
    "FreezeProcessesGroupAction",
    "create_default_registry",
    "FaultEngine",
]
