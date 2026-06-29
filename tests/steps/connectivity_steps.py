"""Shared connectivity check steps used across partition action tests.

Uses ping for symmetric (bidirectional) connectivity checks.
Uses UDP (ncat) for directional checks — send a packet one way,
verify if it arrives without requiring a response.
"""

import subprocess
import time

from behave import then

from faultstorm.cluster import ClusterManager


# ---------------------------------------------------------------------------
# Helpers — symmetric (ping-based)
# ---------------------------------------------------------------------------

def can_reach(source_node: str, target_node: str, timeout: int = 2) -> bool:
    """Check if source_node can reach target_node via ping (bidirectional)."""
    try:
        ClusterManager.exec_on_node(
            source_node,
            ["ping", "-c", "1", "-W", str(timeout), target_node],
            timeout=timeout + 5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def assert_can_reach(source: str, target: str):
    """Assert source can reach target (symmetric, via ping)."""
    assert can_reach(source, target), (
        f"{source} cannot reach {target}, but should be able to"
    )


def assert_cannot_reach(source: str, target: str):
    """Assert source cannot reach target (symmetric, via ping)."""
    assert not can_reach(source, target), (
        f"{source} can reach {target}, but should NOT be able to"
    )


# ---------------------------------------------------------------------------
# Helpers — directional (UDP-based)
# ---------------------------------------------------------------------------

_UDP_PORT = 44444


def can_send_udp(source_node: str, target_node: str,
                 port: int = _UDP_PORT, timeout: int = 2) -> bool:
    """Check if a UDP packet sent from source_node arrives at target_node.

    Starts a one-shot UDP listener on target_node, sends a packet from
    source_node, checks if listener received data.

    This is truly directional: only tests source→target path.

    Args:
        source_node: Node that sends the UDP packet
        target_node: Node that receives the UDP packet
        port: UDP port to use
        timeout: Timeout in seconds

    Returns:
        True if the UDP packet was received, False otherwise
    """
    target_container = ClusterManager._container_name(target_node)
    source_container = ClusterManager._container_name(source_node)

    # Clean up any previous test artifacts
    subprocess.run(
        ["docker", "exec", target_container, "rm", "-f", "/tmp/udp_result"],
        check=False, capture_output=True, timeout=5,
    )
    subprocess.run(
        ["docker", "exec", target_container,
         "sh", "-c", f"pkill -f 'ncat -u -l {port}' 2>/dev/null"],
        check=False, capture_output=True, timeout=5,
    )

    # Start one-shot UDP listener in background
    # It writes received data to /tmp/udp_result and exits
    subprocess.run(
        ["docker", "exec", "-d", target_container,
         "sh", "-c",
         f"timeout {timeout + 1} ncat -u -l {port} --recv-only -i 1 > /tmp/udp_result 2>/dev/null"],
        check=False, timeout=5,
    )
    time.sleep(0.3)

    # Send UDP packet from source
    try:
        subprocess.run(
            ["docker", "exec", source_container,
             "sh", "-c",
             f"echo 'UDPOK' | ncat -u -w 1 {target_node} {port}"],
            check=False, timeout=timeout + 3, capture_output=True,
        )
    except subprocess.TimeoutExpired:
        pass

    time.sleep(1.0)

    # Check if listener received data
    try:
        result = subprocess.run(
            ["docker", "exec", target_container, "cat", "/tmp/udp_result"],
            capture_output=True, text=True, timeout=5,
        )
        received = result.stdout.strip()
        return "UDPOK" in received
    except Exception:
        return False
    finally:
        # Cleanup
        subprocess.run(
            ["docker", "exec", target_container,
             "sh", "-c",
             f"pkill -f 'ncat -u -l {port}' 2>/dev/null; rm -f /tmp/udp_result"],
            check=False, capture_output=True, timeout=5,
        )


def assert_can_send_to(source: str, target: str):
    """Assert source can send UDP to target (directional)."""
    assert can_send_udp(source, target), (
        f"{source} cannot send UDP to {target}, but should be able to"
    )


def assert_cannot_send_to(source: str, target: str):
    """Assert source cannot send UDP to target (directional)."""
    assert not can_send_udp(source, target), (
        f"{source} can send UDP to {target}, but should NOT be able to"
    )


# ---------------------------------------------------------------------------
# Common steps used by multiple partition actions
# ---------------------------------------------------------------------------

@then('all nodes can reach each other')
def step_all_nodes_can_reach(context):
    nodes = context.all_nodes
    for src in nodes:
        for dst in nodes:
            if src != dst:
                assert_can_reach(src, dst)


@then('the load node cannot reach "{node}"')
def step_load_cannot_reach_node(context, node):
    assert_cannot_reach(context.load_node, node)


@then('node "{node}" cannot reach the load node')
def step_node_cannot_reach_load(context, node):
    assert_cannot_reach(node, context.load_node)


@then('the load node can reach "{node}"')
def step_load_can_reach_node(context, node):
    assert_can_reach(context.load_node, node)


@then('node "{node}" can reach the load node')
def step_node_can_reach_load(context, node):
    assert_can_reach(node, context.load_node)
