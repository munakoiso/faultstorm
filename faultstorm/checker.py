"""
Consistency checker for FaultStorm tests.

Implements set-based consistency checking: compares attempted writes,
confirmed writes, and final reads to detect data loss, corruption,
and recovered (indeterminate) writes.
"""

import json
import logging
import math
from datetime import datetime, timezone
from typing import List, Set

from faultstorm.model import CheckResult

logger = logging.getLogger(__name__)

# Operation types (matching load_generator output)
INVOKE = "invoke"
OK = "ok"
FAIL = "fail"
INFO = "info"


def _fmt_ts(ts: float) -> str:
    """Format a Unix timestamp as a human-readable UTC string with 0.1 s precision."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 100000}"


def _compute_interval_availability(
    write_invoke_times: List[float],
    ok_write_times: List[float],
    interval: float = 0.1,
) -> float:
    """Compute write availability using time-interval bucketing.

    Divides the time span from the first write attempt to the last write
    attempt into fixed-size intervals and checks whether each interval
    contains at least one successful write.

    Availability = (number of intervals with ≥1 successful write)
                   / (total number of intervals)

    Args:
        write_invoke_times: Timestamps of all write invocations.
        ok_write_times: Timestamps of all successful writes.
        interval: Interval length in seconds (default 0.1 s)

    Returns:
        Availability as a float in [0.0, 1.0].  Returns 0.0 when there
        are no write attempts.
    """
    if not write_invoke_times:
        return 0.0

    start_time = min(write_invoke_times)
    end_time = max(write_invoke_times)
    logger.info("Write start time: %s, end time: %s", _fmt_ts(start_time), _fmt_ts(end_time))

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
                    _fmt_ts(unavailable_since * interval + start_time),
                    _fmt_ts(idx * interval + start_time),
                )
                unavailable_since = None
        else:
            if unavailable_since is None:
                unavailable_since = idx
        ts += interval

    return len(available_intervals) / total_intervals


def _log_problem_intervals(
    problem_values: Set[int],
    value_to_invoke_ts: dict[int, float],
    start_time: float,
    end_time: float,
    label: str,
    interval: float = 0.1,
) -> None:
    """Log time intervals where problematic writes were attempted.

    For a given set of problematic values (e.g. lost or unexpected),
    looks up the invoke timestamp for each value, buckets them into
    fixed-size intervals, finds contiguous ranges of intervals that
    contained at least one problematic write attempt, and logs each
    range via ``logger.debug``.

    Args:
        problem_values: Set of values that had problems (lost / unexpected).
        value_to_invoke_ts: Mapping from value to its invoke timestamp.
        start_time: Earliest invoke timestamp (defines bucket origin).
        end_time: Latest invoke timestamp.
        label: Human-readable label used in log messages (e.g. "Lost writes").
        interval: Bucket width in seconds (default 0.1 s).
    """
    if not problem_values:
        return

    total_intervals = max(1, math.ceil((end_time - start_time) / interval))

    # Collect interval indices that contain at least one problematic invoke.
    problem_indices: set[int] = set()
    for val in problem_values:
        ts = value_to_invoke_ts.get(val)
        if ts is None:
            continue
        idx = int((ts - start_time) / interval)
        idx = max(0, min(idx, total_intervals - 1))
        problem_indices.add(idx)

    if not problem_indices:
        return

    # Walk through all intervals and log contiguous problem ranges.
    problem_since: float | None = None
    for idx in range(total_intervals):
        if idx in problem_indices:
            if problem_since is None:
                problem_since = start_time + idx * interval
        else:
            if problem_since is not None:
                logger.debug(
                    "%s from %s to %s",
                    label,
                    _fmt_ts(problem_since),
                    _fmt_ts(start_time + idx * interval),
                )
                problem_since = None
    # Flush trailing range.
    if problem_since is not None:
        logger.debug(
            "%s from %s to %s",
            label,
            _fmt_ts(problem_since),
            _fmt_ts(start_time + total_intervals * interval),
        )


def check_consistency(operations_log: str) -> CheckResult:
    """Check data consistency from an operations log.

    Streams the operations log line-by-line to avoid loading the entire
    file into memory — critical for long tests that produce hundreds of
    megabytes of JSON.

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
    attempted: Set[int] = set()  # All values we tried to write (INVOKE)
    successful: Set[int] = set()  # Values confirmed written (OK)
    failed: Set[int] = set()  # Values confirmed NOT written (FAIL)
    # INFO values are in attempted but not in successful or failed
    final_read: Set[int] = set()
    read_count = 0
    value_to_invoke_ts: dict[int, float] = {}  # value → invoke timestamp

    # Collect timestamps for availability calculation during the same pass.
    write_invoke_times: List[float] = []
    ok_write_times: List[float] = []

    line_count = 0

    with open(operations_log, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                op = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed line: %s (%s)", line[:200], e)
                continue

            line_count += 1

            op_type = op.get("type", "")
            action = op.get("action", "")
            value = op.get("value")
            ts = op.get("timestamp")

            if action == "add":
                if value is None:
                    continue
                int_value = int(value)

                if op_type == INVOKE:
                    attempted.add(int_value)
                    if ts is not None:
                        float_ts = float(ts)
                        value_to_invoke_ts[int_value] = float_ts
                        write_invoke_times.append(float_ts)
                elif op_type == OK:
                    successful.add(int_value)
                    if ts is not None:
                        ok_write_times.append(float(ts))
                elif op_type == FAIL:
                    failed.add(int_value)
                # INFO: indeterminate — already in attempted, not in successful/failed

            elif action == "read":
                if op_type == OK and value is not None:
                    if isinstance(value, list):
                        # Overwrite on each successful read so that only the last
                        # read snapshot is used for consistency checking.  Multiple
                        # reads may occur during the read phase; we keep the final
                        # one because it is the most up-to-date view of the data.
                        final_read = set(value)
                        read_count += 1

    logger.info("Streamed %d lines from operations log", line_count)

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

    write_availability = _compute_interval_availability(write_invoke_times, ok_write_times)

    # Log time intervals where problematic writes were attempted.
    if value_to_invoke_ts:
        invoke_times = list(value_to_invoke_ts.values())
        invoke_start = min(invoke_times)
        invoke_end = max(invoke_times)
        _log_problem_intervals(lost, value_to_invoke_ts, invoke_start, invoke_end, "Lost writes")
        _log_problem_intervals(
            unexpected, value_to_invoke_ts, invoke_start, invoke_end, "Unexpected writes"
        )

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
