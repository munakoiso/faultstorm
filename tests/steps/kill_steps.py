"""Step definitions for KillProcessAction tests."""

import subprocess
import time

from behave import given, when, then

from faultstorm.cluster import ClusterManager
from faultstorm.faults.actions import KillProcessAction


def _start_process(node, command):
    """Start a background process on a node via docker exec -d."""
    container = ClusterManager._container_name(node)
    cmd = ["docker", "exec", "-d", container] + command.split()
    subprocess.run(cmd, check=True, timeout=10)
    time.sleep(0.5)  # give the process time to start


def _is_process_running(node, process_name):
    """Check if a process is running on a node."""
    try:
        output = ClusterManager.exec_on_node(
            node, ["pgrep", "-x", process_name], timeout=10
        )
        return bool(output.strip())
    except Exception:
        return False


@given('a background process "{command}" is running on node "{node}"')
def step_start_process_on_node(context, command, node):
    _start_process(node, command)
    process_name = command.split()[0]
    assert _is_process_running(node, process_name), (
        f"Failed to start process '{process_name}' on {node}"
    )


@given('a background process "{command}" is running on all db nodes')
def step_start_process_on_all_db(context, command):
    for node in context.db_nodes:
        _start_process(node, command)
    process_name = command.split()[0]
    for node in context.db_nodes:
        assert _is_process_running(node, process_name), (
            f"Failed to start process '{process_name}' on {node}"
        )


@when('I execute a kill action for process "{process}" on node "{node}"')
def step_execute_kill_specific(context, process, node):
    action = KillProcessAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        process=process, node=node, processes=[process]
    )
    action.execute()
    time.sleep(0.5)  # give pkill time to take effect
    context.action = action


@when('I execute a kill action for process "{process}" with no target node')
def step_execute_kill_random_node(context, process):
    action = KillProcessAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        process=process, node=None, processes=[process]
    )
    action.execute()
    time.sleep(0.5)  # give pkill time to take effect
    context.action = action


@then('the process "{process}" is not running on node "{node}"')
def step_check_process_not_running(context, process, node):
    assert not _is_process_running(node, process), (
        f"Process '{process}' is still running on {node}"
    )


@then('the process "{process}" is not running on exactly one db node')
def step_check_process_killed_on_one(context, process):
    killed_nodes = [
        node for node in context.db_nodes
        if not _is_process_running(node, process)
    ]
    assert len(killed_nodes) == 1, (
        f"Expected process '{process}' killed on exactly 1 db node, "
        f"but killed on {len(killed_nodes)}: {killed_nodes}"
    )


# ---- Serialization ----


@given('a kill action with ordinal {ordinal:d}, process "{process}" and node "{node}"')
def step_given_kill_action(context, ordinal, process, node):
    context.action = KillProcessAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        process=process, node=node
    )


@when('I serialize and deserialize the kill action')
def step_serialize_deserialize_kill(context):
    serialized = context.action.serialize()
    context.deserialized = KillProcessAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized kill action has ordinal {ordinal:d}, process "{process}" and node "{node}"')
def step_check_deserialized_kill(context, ordinal, process, node):
    action = context.deserialized
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.process == process, (
        f"Expected process '{process}', got '{action.process}'"
    )
    assert action.node == node, (
        f"Expected node '{node}', got '{action.node}'"
    )
