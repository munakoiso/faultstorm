"""Step definitions for PartitionRandomDcAction tests."""

from behave import given, when, then

from faultstorm.faults.actions import PartitionRandomDcAction
from steps.connectivity_steps import assert_can_reach, assert_cannot_reach


@when('I execute a partition_random_dc action isolating DC "{dc_name}"')
@given('I execute a partition_random_dc action isolating DC "{dc_name}"')
def step_execute_partition_dc(context, dc_name):
    action = PartitionRandomDcAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        dc_map=context.dc_map,
        dc_name=dc_name,
    )
    action.execute()
    context.action = action
    context.isolated_dc = dc_name
    context.dc_nodes = context.dc_map.get(dc_name, [])
    context.outside_dc_nodes = [
        n for n in context.all_nodes
        if n not in context.dc_nodes
    ]


@then('nodes in DC "{dc_name}" cannot reach nodes outside DC "{dc_name2}"')
def step_dc_cannot_reach_outside(context, dc_name, dc_name2):
    dc_nodes = context.dc_map.get(dc_name, [])
    outside = [n for n in context.all_nodes if n not in dc_nodes]
    for src in dc_nodes:
        for dst in outside:
            assert_cannot_reach(src, dst)


@then('nodes outside DC "{dc_name}" cannot reach nodes in DC "{dc_name2}"')
def step_outside_cannot_reach_dc(context, dc_name, dc_name2):
    dc_nodes = context.dc_map.get(dc_name, [])
    outside = [n for n in context.all_nodes if n not in dc_nodes]
    for src in outside:
        for dst in dc_nodes:
            assert_cannot_reach(src, dst)


@then('nodes within DC "{dc_name}" can reach each other')
def step_within_dc_can_reach(context, dc_name):
    dc_nodes = context.dc_map.get(dc_name, [])
    for src in dc_nodes:
        for dst in dc_nodes:
            if src != dst:
                assert_can_reach(src, dst)


@then('the load node can reach nodes in DC "{dc_name}"')
def step_load_can_reach_dc(context, dc_name):
    dc_nodes = context.dc_map.get(dc_name, [])
    for node in dc_nodes:
        assert_can_reach(context.load_node, node)


@then('nodes in DC "{dc_name}" can reach the load node')
def step_dc_can_reach_load(context, dc_name):
    dc_nodes = context.dc_map.get(dc_name, [])
    for node in dc_nodes:
        assert_can_reach(node, context.load_node)


@when('I heal the partition_random_dc action')
def step_heal_partition_dc(context):
    context.action.heal()


@when('I execute a partition_random_dc action with empty dc_map')
def step_execute_dc_empty_map(context):
    action = PartitionRandomDcAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        dc_map={},
    )
    action.execute()
    context.action = action


# ---- Serialization ----

@given('a partition_random_dc action with ordinal {ordinal:d} and dc_name "{dc_name}"')
def step_given_dc_action(context, ordinal, dc_name):
    context.action = PartitionRandomDcAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        dc_map=context.dc_map, dc_name=dc_name,
    )


@when('I serialize and deserialize the partition_random_dc action')
def step_serialize_dc(context):
    serialized = context.action.serialize()
    context.deserialized = PartitionRandomDcAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes,
        dc_map=context.dc_map,
    )


@then('the deserialized action has ordinal {ordinal:d} and dc_name "{dc_name}"')
def step_check_deserialized_dc(context, ordinal, dc_name):
    action = context.deserialized
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.dc_name == dc_name, (
        f"Expected dc_name '{dc_name}', got '{action.dc_name}'"
    )
