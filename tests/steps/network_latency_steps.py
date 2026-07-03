"""Step definitions for NetworkLatencyManager tests."""

import re

from behave import given, then, when

from faultstorm.cluster import ClusterManager
from faultstorm.config import TestConfig
from faultstorm.network_latency import NetworkLatencyManager


def _measure_ping_ms(source_node: str, target_node: str, count: int = 3) -> float:
    """Measure average ping RTT from source_node to target_node.

    Args:
        source_node: Node to ping from.
        target_node: Node to ping to.
        count: Number of ping packets.

    Returns:
        Average RTT in milliseconds.
    """
    output = ClusterManager.exec_on_node(
        source_node,
        ["ping", "-c", str(count), "-W", "5", target_node],
        timeout=count * 5 + 10,
    )
    # Parse "rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms"
    match = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", output)
    if not match:
        # Try alternative format: "round-trip min/avg/max = ..."
        match = re.search(r"round-trip min/avg/max(?:/\w+)? = [\d.]+/([\d.]+)/", output)
    assert match, f"Cannot parse ping output from {source_node} -> {target_node}:\n{output}"
    return float(match.group(1))


def _make_config(context, cross_dc_delays=None, db_zk_delay_ms=0):
    """Create a TestConfig with the given latency settings."""
    return TestConfig(
        db_nodes=list(context.db_nodes),
        extra_nodes=list(context.extra_nodes),
        cross_dc_delays=cross_dc_delays or {},
        db_zk_delay_ms=db_zk_delay_ms,
    )


@given('a network latency config with cross-DC delay {delay:d}ms between "{dc_a}" and "{dc_b}"')
def step_config_cross_dc(context, delay, dc_a, dc_b):
    config = _make_config(context, cross_dc_delays={(dc_a, dc_b): delay})
    context.latency_manager = NetworkLatencyManager(config)


@given("a network latency config with db-zk delay {delay:d}ms")
def step_config_db_zk(context, delay):
    config = _make_config(context, db_zk_delay_ms=delay)
    context.latency_manager = NetworkLatencyManager(config)


@given(
    'a network latency config with cross-DC delay {dc_delay:d}ms'
    ' between "{dc_a}" and "{dc_b}" and db-zk delay {zk_delay:d}ms'
)
def step_config_cross_dc_and_db_zk(context, dc_delay, dc_a, dc_b, zk_delay):
    config = _make_config(
        context,
        cross_dc_delays={(dc_a, dc_b): dc_delay},
        db_zk_delay_ms=zk_delay,
    )
    context.latency_manager = NetworkLatencyManager(config)


@when("I apply network latency")
def step_apply_latency(context):
    context.latency_manager.apply(context.dc_map)


@when("I remove network latency")
def step_remove_latency(context):
    context.latency_manager.remove()


@then('ping from "{source}" to "{target}" takes at least {threshold:d}ms')
def step_ping_at_least(context, source, target, threshold):
    avg_ms = _measure_ping_ms(source, target)
    assert avg_ms >= threshold * 0.8, (
        f"Ping {source} -> {target}: avg {avg_ms:.1f}ms is below "
        f"expected threshold {threshold}ms (with 20% tolerance: {threshold * 0.8:.1f}ms)"
    )


@then('ping from "{source}" to "{target}" takes less than {threshold:d}ms')
def step_ping_less_than(context, source, target, threshold):
    avg_ms = _measure_ping_ms(source, target)
    assert avg_ms < threshold, (
        f"Ping {source} -> {target}: avg {avg_ms:.1f}ms is not below {threshold}ms"
    )
