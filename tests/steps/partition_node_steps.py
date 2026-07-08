"""Step definitions for PartitionRandomNodeAction tests."""

from behave import given, when, then

from faultstorm.faults.actions import PartitionRandomNodeAction
from steps.connectivity_steps import assert_can_reach, assert_cannot_reach


@when('I execute a partition_random_node action isolating "{node}"')
@given('I execute a partition_random_node action isolating "{node}"')
def step_execute_partition_node(context, node):
    action = PartitionRandomNodeAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        dc_map=context.dc_map,
        node=node,
    )
    action.execute()
    context.action = action
    context.isolated_node = node


@then('node "{node}" cannot reach any other node')
def step_isolated_cannot_reach_others(context, node):
    others = [n for n in context.all_nodes if n != node]
    for target in others:
        assert_cannot_reach(node, target)


@then('no other node can reach "{node}"')
def step_others_cannot_reach_isolated(context, node):
    others = [n for n in context.all_nodes if n != node]
    for src in others:
        assert_cannot_reach(src, node)


@then('nodes other than "{node}" can reach each other')
def step_others_can_reach_each_other(context, node):
    others = [n for n in context.all_nodes if n != node]
    for src in others:
        for dst in others:
            if src != dst:
                assert_can_reach(src, dst)


@when('I heal the partition_random_node action')
def step_heal_partition_node(context):
    context.action.heal()


# ---- Serialization ----

@given('a partition_random_node action with ordinal {ordinal:d} and node "{node}"')
def step_given_partition_node_action(context, ordinal, node):
    context.action = PartitionRandomNodeAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        node=node,
    )


@when('I serialize and deserialize the partition_random_node action')
def step_serialize_partition_node(context):
    serialized = context.action.serialize()
    context.deserialized = PartitionRandomNodeAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized action has ordinal {ordinal:d} and node "{node}"')
def step_check_deserialized_partition_node(context, ordinal, node):
    action = context.deserialized
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.node == node, (
        f"Expected node '{node}', got '{action.node}'"
    )
