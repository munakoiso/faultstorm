"""Step definitions for FreezeProcessesAction and FreezeProcessesGroupAction tests."""

import subprocess
import time

from behave import given, when, then

from faultstorm.cluster import ClusterManager
from faultstorm.faults.actions import FreezeProcessesAction, FreezeProcessesGroupAction


FLAG_FILE = FreezeProcessesAction.FLAG_FILE
LOG_FILE = "/var/log/process_freezer.log"


def _start_freezer_daemon(node):
    """Start the process_freezer.sh daemon on a node in background."""
    container = ClusterManager._container_name(node)
    # Check if already running
    try:
        result = subprocess.run(
            ["docker", "exec", container, "pgrep", "-f", "process_freezer.sh"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return  # already running
    except Exception:
        pass
    # Start in background
    subprocess.run(
        ["docker", "exec", "-d", container,
         "bash", "-c", "FREEZER_LOG_FILE=/var/log/process_freezer.log /usr/local/bin/process_freezer.sh"],
        check=True, timeout=10,
    )
    time.sleep(0.5)


def _flag_exists(node):
    """Check if the freeze flag file exists on a node."""
    try:
        ClusterManager.exec_on_node(node, ["test", "-f", FLAG_FILE], timeout=5)
        return True
    except Exception:
        return False


def _flag_content(node):
    """Read the freeze flag file content on a node."""
    try:
        return ClusterManager.exec_on_node(
            node, ["cat", FLAG_FILE], timeout=5
        )
    except Exception:
        return ""


def _freezer_log(node):
    """Read the freezer log file content on a node."""
    try:
        return ClusterManager.exec_on_node(
            node, ["cat", LOG_FILE], timeout=5
        )
    except Exception:
        return ""


def _is_process_running(node, process_name):
    """Check if a process is running on a node."""
    try:
        output = ClusterManager.exec_on_node(
            node, ["pgrep", "-x", process_name], timeout=10
        )
        return bool(output.strip())
    except Exception:
        return False


# ---- Background ----


@given('the process freezer daemon is running on all db nodes')
def step_start_freezer_on_db(context):
    for node in context.db_nodes:
        _start_freezer_daemon(node)


@given('the process freezer daemon is running on all nodes')
def step_start_freezer_on_all(context):
    for node in context.db_nodes + context.extra_nodes:
        _start_freezer_daemon(node)


# ---- FreezeProcessesAction steps ----


@when('I execute a freeze action for processes "{processes}" on node "{node}"')
def step_execute_freeze_specific(context, processes, node):
    proc_list = processes.split(',')
    action = FreezeProcessesAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        node=node, processes=proc_list
    )
    action.execute()
    time.sleep(0.5)
    context.freeze_action = action


@when('I execute a freeze action for processes "{processes}" with no target node')
def step_execute_freeze_random(context, processes):
    proc_list = processes.split(',')
    action = FreezeProcessesAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        node=None, processes=proc_list
    )
    action.execute()
    time.sleep(0.5)
    context.freeze_action = action


@when('I heal the freeze action')
def step_heal_freeze(context):
    context.freeze_action.heal()
    time.sleep(0.5)


@when('I wait for {seconds:d} seconds')
def step_wait_seconds(context, seconds):
    time.sleep(seconds)


@then('the freeze flag file exists on node "{node}"')
def step_flag_exists(context, node):
    assert _flag_exists(node), (
        f"Freeze flag file does not exist on {node}"
    )


@then('the freeze flag file does not exist on node "{node}"')
def step_flag_not_exists(context, node):
    assert not _flag_exists(node), (
        f"Freeze flag file still exists on {node}"
    )


@then('the freeze flag file contains "{pattern}" on node "{node}"')
def step_flag_contains(context, pattern, node):
    content = _flag_content(node)
    assert pattern in content, (
        f"Freeze flag file on {node} does not contain '{pattern}'. "
        f"Content: {content!r}"
    )


@then('the freeze flag file exists on exactly one db node')
def step_flag_on_one_db(context):
    nodes_with_flag = [
        node for node in context.db_nodes
        if _flag_exists(node)
    ]
    assert len(nodes_with_flag) == 1, (
        f"Expected freeze flag on exactly 1 db node, "
        f"but found on {len(nodes_with_flag)}: {nodes_with_flag}"
    )


@then('the process "{process}" on node "{node}" was frozen at least once')
def step_process_was_frozen(context, process, node):
    log = _freezer_log(node)
    assert "SIGSTOP" in log, (
        f"Freezer log on {node} does not contain any SIGSTOP entries. "
        f"Log: {log[:500]}"
    )


@then('the process "{process}" is still running on node "{node}"')
def step_process_still_running(context, process, node):
    assert _is_process_running(node, process), (
        f"Process '{process}' is not running on {node}"
    )


# ---- Serialization ----


@given('a freeze action with ordinal {ordinal:d}, node "{node}" and processes "{processes}"')
def step_given_freeze(context, ordinal, node, processes):
    proc_list = processes.split(',')
    context.freeze_action = FreezeProcessesAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        node=node, processes=proc_list
    )


@given('a freeze action with ordinal {ordinal:d}, node "{node}", processes "{processes}", freeze range {fmin:d}-{fmax:d} and pause range {pmin:d}-{pmax:d}')
def step_given_freeze_with_ranges(context, ordinal, node, processes, fmin, fmax, pmin, pmax):
    proc_list = processes.split(',')
    context.freeze_action = FreezeProcessesAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        node=node, processes=proc_list,
        freeze_duration_range=(fmin, fmax),
        freeze_pause_range=(pmin, pmax),
    )


@when('I serialize and deserialize the freeze action')
def step_serde_freeze(context):
    serialized = context.freeze_action.serialize()
    context.deserialized = FreezeProcessesAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized freeze action has ordinal {ordinal:d}, node "{node}" and processes "{processes}"')
def step_check_serde_freeze(context, ordinal, node, processes):
    action = context.deserialized
    proc_list = processes.split(',')
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.node == node, (
        f"Expected node '{node}', got '{action.node}'"
    )
    assert action.processes == proc_list, (
        f"Expected processes {proc_list}, got {action.processes}"
    )


@then('the deserialized freeze action has freeze range {fmin:d}-{fmax:d} and pause range {pmin:d}-{pmax:d}')
def step_check_serde_freeze_ranges(context, fmin, fmax, pmin, pmax):
    action = context.deserialized
    assert action.freeze_duration_range == (fmin, fmax), (
        f"Expected freeze_duration_range ({fmin}, {fmax}), "
        f"got {action.freeze_duration_range}"
    )
    assert action.freeze_pause_range == (pmin, pmax), (
        f"Expected freeze_pause_range ({pmin}, {pmax}), "
        f"got {action.freeze_pause_range}"
    )


# ---- FreezeProcessesGroupAction steps ----


@when('I execute a group freeze action for processes "{processes}" on group "{group}"')
def step_execute_group_freeze(context, processes, group):
    proc_list = processes.split(',')
    action = FreezeProcessesGroupAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        group=group, processes=proc_list
    )
    action.execute()
    time.sleep(0.5)
    context.group_freeze_action = action


@when('I execute a group freeze action for processes "{processes}" with no target group')
def step_execute_group_freeze_random(context, processes):
    proc_list = processes.split(',')
    action = FreezeProcessesGroupAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        group=None, processes=proc_list
    )
    action.execute()
    time.sleep(0.5)
    context.group_freeze_action = action


@when('I heal the group freeze action')
def step_heal_group_freeze(context):
    context.group_freeze_action.heal()
    time.sleep(0.5)


@then('the freeze flag file exists on all db nodes')
def step_flag_on_all_db(context):
    for node in context.db_nodes:
        assert _flag_exists(node), (
            f"Freeze flag file does not exist on DB node {node}"
        )


@then('the freeze flag file exists on all extra nodes')
def step_flag_on_all_extra(context):
    for node in context.extra_nodes:
        assert _flag_exists(node), (
            f"Freeze flag file does not exist on extra node {node}"
        )


@then('the freeze flag file does not exist on any db node')
def step_flag_not_on_any_db(context):
    for node in context.db_nodes:
        assert not _flag_exists(node), (
            f"Freeze flag file still exists on DB node {node}"
        )


@then('the freeze flag file does not exist on any extra node')
def step_flag_not_on_any_extra(context):
    for node in context.extra_nodes:
        assert not _flag_exists(node), (
            f"Freeze flag file still exists on extra node {node}"
        )


@then('the freeze flag file exists on all nodes of exactly one group')
def step_flag_on_exactly_one_group(context):
    db_flags = all(_flag_exists(n) for n in context.db_nodes)
    extra_flags = all(_flag_exists(n) for n in context.extra_nodes)
    assert db_flags != extra_flags, (
        f"Expected freeze flag on exactly one group. "
        f"DB all={db_flags}, extra all={extra_flags}"
    )


# ---- Group serialization ----


@given('a group freeze action with ordinal {ordinal:d}, group "{group}" and processes "{processes}"')
def step_given_group_freeze(context, ordinal, group, processes):
    proc_list = processes.split(',')
    context.group_freeze_action = FreezeProcessesGroupAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        group=group, processes=proc_list
    )


@given('a group freeze action with ordinal {ordinal:d}, group "{group}", processes "{processes}", freeze range {fmin:d}-{fmax:d} and pause range {pmin:d}-{pmax:d}')
def step_given_group_freeze_with_ranges(context, ordinal, group, processes, fmin, fmax, pmin, pmax):
    proc_list = processes.split(',')
    context.group_freeze_action = FreezeProcessesGroupAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        group=group, processes=proc_list,
        freeze_duration_range=(fmin, fmax),
        freeze_pause_range=(pmin, pmax),
    )


@when('I serialize and deserialize the group freeze action')
def step_serde_group_freeze(context):
    serialized = context.group_freeze_action.serialize()
    context.deserialized_group = FreezeProcessesGroupAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized group freeze action has ordinal {ordinal:d}, group "{group}" and processes "{processes}"')
def step_check_serde_group_freeze(context, ordinal, group, processes):
    action = context.deserialized_group
    proc_list = processes.split(',')
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.group == group, (
        f"Expected group '{group}', got '{action.group}'"
    )
    assert action.processes == proc_list, (
        f"Expected processes {proc_list}, got {action.processes}"
    )


@then('the deserialized group freeze action has freeze range {fmin:d}-{fmax:d} and pause range {pmin:d}-{pmax:d}')
def step_check_serde_group_freeze_ranges(context, fmin, fmax, pmin, pmax):
    action = context.deserialized_group
    assert action.freeze_duration_range == (fmin, fmax), (
        f"Expected freeze_duration_range ({fmin}, {fmax}), "
        f"got {action.freeze_duration_range}"
    )
    assert action.freeze_pause_range == (pmin, pmax), (
        f"Expected freeze_pause_range ({pmin}, {pmax}), "
        f"got {action.freeze_pause_range}"
    )
