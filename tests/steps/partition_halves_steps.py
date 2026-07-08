"""Step definitions for PartitionRandomHalvesAction tests."""

from behave import given, when, then

from faultstorm.faults.actions import PartitionRandomHalvesAction
from steps.connectivity_steps import assert_can_reach, assert_cannot_reach


@given('nodes are split into group1 "{g1_csv}" and group2 "{g2_csv}"')
def step_split_groups(context, g1_csv, g2_csv):
    context.group1 = g1_csv.split(',')
    context.group2 = g2_csv.split(',')


@when('I execute a partition_random_halves action with these groups')
@given('I execute a partition_random_halves action with these groups')
def step_execute_partition_halves(context):
    action = PartitionRandomHalvesAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        dc_map=context.dc_map,
        group1=context.group1, group2=context.group2,
    )
    action.execute()
    context.action = action


@then('nodes in group1 cannot reach nodes in group2')
def step_group1_cannot_reach_group2(context):
    for src in context.group1:
        for dst in context.group2:
            assert_cannot_reach(src, dst)


@then('nodes in group2 cannot reach nodes in group1')
def step_group2_cannot_reach_group1(context):
    for src in context.group2:
        for dst in context.group1:
            assert_cannot_reach(src, dst)


@then('nodes within group1 can reach each other')
def step_group1_internal(context):
    for src in context.group1:
        for dst in context.group1:
            if src != dst:
                assert_can_reach(src, dst)


@then('nodes within group2 can reach each other')
def step_group2_internal(context):
    for src in context.group2:
        for dst in context.group2:
            if src != dst:
                assert_can_reach(src, dst)


@when('I heal the partition_random_halves action')
def step_heal_partition_halves(context):
    context.action.heal()


# ---- Serialization ----

@given('a partition_random_halves action with ordinal {ordinal:d}, group1 "{g1_csv}" and group2 "{g2_csv}"')
def step_given_halves_action(context, ordinal, g1_csv, g2_csv):
    context.action = PartitionRandomHalvesAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        group1=g1_csv.split(','), group2=g2_csv.split(','),
    )


@when('I serialize and deserialize the partition_random_halves action')
def step_serialize_halves(context):
    serialized = context.action.serialize()
    context.deserialized = PartitionRandomHalvesAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized action has the same groups')
def step_check_deserialized_halves(context):
    orig = context.action
    deser = context.deserialized
    assert deser.ordinal == orig.ordinal, (
        f"Ordinal mismatch: {deser.ordinal} != {orig.ordinal}"
    )
    assert deser.group1 == orig.group1, (
        f"Group1 mismatch: {deser.group1} != {orig.group1}"
    )
    assert deser.group2 == orig.group2, (
        f"Group2 mismatch: {deser.group2} != {orig.group2}"
    )
