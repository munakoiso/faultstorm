"""
Consistency checker for FaultStorm tests.

Implements set-based consistency checking: compares attempted writes,
confirmed writes, and final reads to detect data loss, corruption,
and recovered (indeterminate) writes.
"""

import json
import logging
import math
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


def _compute_interval_availability(
    operations: List[dict[str, Any]], interval: float = 0.1
) -> float:
    """Compute write availability using time-interval bucketing.

    Divides the time span from the first write attempt (INVOKE with action
    "add") to the last write attempt into fixed-size intervals and checks
    whether each interval contains at least one successful write (OK with
    action "add").

    Availability = (number of intervals with ≥1 successful write)
                   / (total number of intervals)

    Args:
        operations: Parsed operations list (each element has "type",
            "action", "timestamp", etc.)
        interval: Interval length in seconds (default 0.1 s)

    Returns:
        Availability as a float in [0.0, 1.0].  Returns 0.0 when there
        are no write attempts.
    """
    # Collect timestamps for all write invocations and successful writes.
    write_invoke_times: List[float] = []
    ok_write_times: List[float] = []

    for op in operations:
        action = op.get("action", "")
        if action != "add":
            continue
        op_type = op.get("type", "")
        ts = op.get("timestamp")
        if ts is None:
            continue
        if op_type == INVOKE:
            write_invoke_times.append(float(ts))
        elif op_type == OK:
            ok_write_times.append(float(ts))

    if not write_invoke_times:
        return 0.0

    start_time = min(write_invoke_times)
    end_time = max(write_invoke_times)
    logger.info("Write start time: %s, end time: %s", start_time, end_time)

    total_intervals = max(1, math.ceil((end_time - start_time) / interval))

    if not ok_write_times:
        return 0.0

    # Build a set of interval indices that contain at least one successful write.
    available_intervals: set[int] = set()
    for ts in ok_write_times:
        idx = int((ts - start_time) / interval)
        # Clamp to valid range (last boundary maps to the final interval).
        idx = min(idx, total_intervals - 1)
        available_intervals.add(idx)

    unavailable_since = None
    ts = start_time
    while ts < end_time:
        idx = int((ts - start_time) / interval)
        # Clamp to valid range (last boundary maps to the final interval).
        idx = min(idx, total_intervals - 1)
        if idx in available_intervals:
            if unavailable_since is not None:
                logger.debug(
                    "Unavailable from %s to %s",
                    unavailable_since * interval + start_time,
                    ts * interval + start_time,
                )
                unavailable_since = None
        else:
            if unavailable_since is None:
                unavailable_since = ts
        ts += interval

    return len(available_intervals) / total_intervals


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

    Write availability is computed using time-interval bucketing: the time
    from the first write attempt to the last is divided into 0.1 s intervals,
    and the cluster is considered available in a given interval if at least
    one successful write occurred during that interval.

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

    write_availability = _compute_interval_availability(operations)

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
