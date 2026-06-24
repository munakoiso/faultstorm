"""
Faults package for FaultStorm.

Provides fault injection actions, a registry for custom actions,
and an engine with random/replay modes.
"""

from faultstorm.faults.actions import (
    FaultAction,
    FaultRegistry,
    WaitAction,
    HealAllAction,
    KillProcessAction,
    SwitchoverAction,
    PartitionRandomHalvesAction,
    PartitionMajoritiesRingAction,
    PartitionRandomNodeAction,
    create_default_registry,
)
from faultstorm.faults.engine import FaultEngine
from faultstorm.faults.partitioners import Partitioners

__all__ = [
    'FaultAction',
    'FaultRegistry',
    'WaitAction',
    'HealAllAction',
    'KillProcessAction',
    'SwitchoverAction',
    'PartitionRandomHalvesAction',
    'PartitionMajoritiesRingAction',
    'PartitionRandomNodeAction',
    'create_default_registry',
    'FaultEngine',
    'Partitioners',
]
