"""
Consistency checker for FaultStorm tests.

Implements set-based consistency checking: compares attempted writes,
confirmed writes, and final reads to detect data loss, corruption,
and recovered (indeterminate) writes.
"""

import json
import logging
from typing import Any, List, Set

from faultstorm.model import CheckResult

logger = logging.getLogger(__name__)

# Operation types (matching load_generator output)
INVOKE = "invoke"
OK = "ok"
FAIL = "fail"
INFO = "info"


def parse_operations_log(log_path: str) -> List[dict[str, Any]]:
    """Parse JSON operations log file.

    Each line is a JSON object with keys: type, action, value, node, timestamp.

    Args:
        log_path: Path to operations log

    Returns:
        List of operation dicts
    """
    operations: List[dict[str, Any]] = []
    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                operations.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed line: %s (%s)", line, e)
    return operations


def check_consistency(operations_log: str) -> CheckResult:
    """Check data consistency from an operations log.

    Analyzes write operations and final reads to detect:
    - Lost values: confirmed written but missing from final read (DATA LOSS)
    - Unexpected values: never attempted but found in final read (CORRUPTION)
    - Recovered values: indeterminate writes that actually went through

    The semantics follow the standard set checker:
    - lost = successful_adds - final_read
    - unexpected = final_read - attempted
    - recovered = (final_read ∩ attempted) - successful_adds

    Args:
        operations_log: Path to the operations log file

    Returns:
        CheckResult with validation details
    """
    operations = parse_operations_log(operations_log)

    attempted: Set[int] = set()  # All values we tried to write (INVOKE)
    successful: Set[int] = set()  # Values confirmed written (OK)
    failed: Set[int] = set()  # Values confirmed NOT written (FAIL)
    # INFO values are in attempted but not in successful or failed
    final_read: Set[int] = set()
    read_count = 0

    for op in operations:
        op_type = op.get("type", "")
        action = op.get("action", "")
        value = op.get("value")
        if value is None:
            continue

        if action == "add":
            int_value = int(value)
            if op_type == INVOKE:
                attempted.add(int_value)
            elif op_type == OK:
                successful.add(int_value)
            elif op_type == FAIL:
                failed.add(int_value)
            # INFO: indeterminate — already in attempted, not in successful/failed

        elif action == "read":
            if op_type == OK:
                if isinstance(value, list):
                    # Overwrite on each successful read so that only the last
                    # read snapshot is used for consistency checking.  Multiple
                    # reads may occur during the read phase; we keep the final
                    # one because it is the most up-to-date view of the data.
                    final_read = set(value)
                    read_count += 1

    if read_count == 0:
        return CheckResult(
            valid=False,
            errors="No successful reads found in operations log",
        )

    # Compute results
    lost = successful - final_read
    unexpected = final_read - attempted
    recovered = (final_read & attempted) - successful

    total_attempts = len(attempted)
    successful_adds = len(successful)
    failed_adds = len(failed)

    write_availability = 0.0
    if total_attempts > 0:
        write_availability = successful_adds / total_attempts

    valid = len(lost) == 0 and len(unexpected) == 0

    result = CheckResult(
        valid=valid,
        lost=lost,
        unexpected=unexpected,
        recovered=recovered,
        total_attempts=total_attempts,
        successful_adds=successful_adds,
        failed_adds=failed_adds,
        write_availability=write_availability,
    )

    if valid:
        logger.info(
            "Consistency check PASSED: %d successful writes, " "%d recovered, availability %.2f%%",
            successful_adds,
            len(recovered),
            write_availability * 100,
        )
    else:
        logger.error("Consistency check FAILED: %d lost, %d unexpected", len(lost), len(unexpected))

    return result
