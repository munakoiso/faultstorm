"""
FaultStorm — a framework for fault-injection testing of distributed databases.

Provides tools to verify data consistency under network partitions,
process crashes, and other failure scenarios.

Usage::

    from faultstorm import TestConfig, TestRunner, DatabaseClient
    from faultstorm.faults import FaultRegistry, create_default_registry
"""

from faultstorm.config import TestConfig
from faultstorm.db_client import DatabaseClient
from faultstorm.model import CheckResult
from faultstorm.network_latency import NetworkLatencyManager
from faultstorm.runner import TestRunner

__all__ = [
    "TestConfig",
    "DatabaseClient",
    "CheckResult",
    "NetworkLatencyManager",
    "TestRunner",
]
