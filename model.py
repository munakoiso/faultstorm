"""
Data models for FaultStorm test results.
"""

from dataclasses import dataclass, field
from typing import Set, Optional


@dataclass
class CheckResult:
    """Result of a consistency check after a fault-injection test.

    Attributes:
        valid: Whether the test passed (no lost or unexpected values)
        lost: Values confirmed written but missing from final read (DATA LOSS)
        unexpected: Values never attempted but found in final read (CORRUPTION)
        recovered: Indeterminate writes that actually went through
        total_attempts: Total number of write attempts
        successful_adds: Number of confirmed successful writes
        failed_adds: Number of confirmed failed writes
        write_availability: Fraction of successful writes
        errors: Optional error message
    """
    valid: bool = True
    lost: Set[int] = field(default_factory=set)
    unexpected: Set[int] = field(default_factory=set)
    recovered: Set[int] = field(default_factory=set)
    total_attempts: int = 0
    successful_adds: int = 0
    failed_adds: int = 0
    write_availability: float = 0.0
    errors: Optional[str] = None
