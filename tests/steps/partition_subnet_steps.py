"""Step definitions for PartitionRandomSubnetAction tests.

Uses UDP-based directional checks to verify INPUT vs OUTPUT filtering.
"""

from behave import given, when, then

from faultstorm.faults.actions import PartitionRandomSubnetAction
from steps.connectivity_steps import (
    assert_can_reach, assert_cannot_reach,
    assert_can_send_to, assert_cannot_send_to,
)


@when('I execute a partition_random_subnet action on "{node}" direction "{direction}" subnet "{subnet}"')
@given('I execute a partition_random_subnet action on "{node}" direction "{direction}" subnet "{subnet}"')
def step_execute_partition_subnet(context, node, direction, subnet):
    action = PartitionRandomSubnetAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        load_node=context.load_node, dc_map=context.dc_map,
        node=node, direction=direction, subnet_type=subnet,
    )
    action.execute()
    context.action = action
    context.subnet_node = node
    context.subnet_direction = direction
    context.subnet_type = subnet
    context.blocked_nodes = list(action.blocked_nodes or [])


# ---- INPUT direction checks (blocked nodes cannot send TO the target) ----

@then('node "{node}" cannot receive traffic from extra nodes')
def step_cannot_receive_from_extra(context, node):
    for extra in context.extra_nodes:
        assert_cannot_send_to(extra, node)


@then('node "{node}" can send traffic to extra nodes')
def step_can_send_to_extra(context, node):
    for extra in context.extra_nodes:
        assert_can_send_to(node, extra)


# ---- OUTPUT direction checks (target cannot send TO blocked nodes) ----

@then('node "{node}" cannot send traffic to other db nodes')
def step_cannot_send_to_db(context, node):
    others = [n for n in context.db_nodes if n != node]
    for dst in others:
        assert_cannot_send_to(node, dst)


@then('node "{node}" cannot send traffic to the load node')
def step_cannot_send_to_load(context, node):
    assert_cannot_send_to(node, context.load_node)


@then('other db nodes can send traffic to "{node}"')
def step_other_db_can_send(context, node):
    others = [n for n in context.db_nodes if n != node]
    for src in others:
        assert_can_send_to(src, node)


@then('the load node can send traffic to "{node}"')
def step_load_can_send(context, node):
    assert_can_send_to(context.load_node, node)


# ---- Both directions checks ----

@then('node "{node}" cannot send traffic to any blocked node')
def step_cannot_send_to_blocked(context, node):
    for blocked in context.blocked_nodes:
        assert_cannot_send_to(node, blocked)


@then('no blocked node can send traffic to "{node}"')
def step_blocked_cannot_send(context, node):
    for blocked in context.blocked_nodes:
        assert_cannot_send_to(blocked, node)


@then('the load node cannot send traffic to "{node}"')
def step_load_cannot_send(context, node):
    assert_cannot_send_to(context.load_node, node)


@when('I heal the partition_random_subnet action')
def step_heal_partition_subnet(context):
    context.action.heal()


# ---- Serialization ----

@given('a partition_random_subnet action with ordinal {ordinal:d}, node "{node}", direction "{direction}", subnet "{subnet}" and blocked "{blocked_csv}"')
def step_given_subnet_action(context, ordinal, node, direction, subnet, blocked_csv):
    blocked = blocked_csv.split(',')
    context.action = PartitionRandomSubnetAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        load_node=context.load_node,
        node=node, direction=direction, subnet_type=subnet,
        blocked_nodes=blocked,
    )


@when('I serialize and deserialize the partition_random_subnet action')
def step_serialize_subnet(context):
    serialized = context.action.serialize()
    context.deserialized = PartitionRandomSubnetAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes,
        load_node=context.load_node,
    )


@then('the deserialized action has the same subnet parameters')
def step_check_deserialized_subnet(context):
    orig = context.action
    deser = context.deserialized
    assert deser.ordinal == orig.ordinal, (
        f"Ordinal: {deser.ordinal} != {orig.ordinal}"
    )
    assert deser.node == orig.node, (
        f"Node: {deser.node} != {orig.node}"
    )
    assert deser.direction == orig.direction, (
        f"Direction: {deser.direction} != {orig.direction}"
    )
    assert deser.subnet_type == orig.subnet_type, (
        f"Subnet type: {deser.subnet_type} != {orig.subnet_type}"
    )
    assert deser.blocked_nodes == orig.blocked_nodes, (
        f"Blocked: {deser.blocked_nodes} != {orig.blocked_nodes}"
    )
