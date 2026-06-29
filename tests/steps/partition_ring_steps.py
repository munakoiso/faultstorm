"""Step definitions for PartitionMajoritiesRingAction tests.

Ring partition uses INPUT-only blocking: each node blocks traffic
FROM non-majority neighbors. Visibility is directional, so we use
UDP to test: "A accepts from B" means UDP from B reaches A.
"""

from behave import given, when, then

from faultstorm.faults.actions import PartitionMajoritiesRingAction
from steps.connectivity_steps import (
    assert_can_reach, assert_can_send_to, assert_cannot_send_to,
)


def _compute_ring_visibility(ordered):
    """Compute which nodes each node accepts traffic from.

    Returns dict mapping node -> (visible_set, blocked_set).
    "visible" = node does NOT block INPUT from these nodes.
    "blocked" = node blocks INPUT from these nodes.
    """
    n = len(ordered)
    majority = (n // 2) + 1
    result = {}
    for i, node in enumerate(ordered):
        visible = {ordered[(i + j) % n] for j in range(majority)}
        blocked = {nd for nd in ordered if nd not in visible}
        result[node] = (visible, blocked)
    return result


@given('an ordered node list "{nodes_csv}"')
def step_ordered_nodes(context, nodes_csv):
    context.ordered_nodes = nodes_csv.split(',')


@when('I execute a partition_majorities_ring action with this order')
@given('I execute a partition_majorities_ring action with this order')
def step_execute_ring(context):
    action = PartitionMajoritiesRingAction(
        context.db_nodes, context.extra_nodes, ordinal=1,
        load_node=context.load_node, dc_map=context.dc_map,
        ordered=context.ordered_nodes,
    )
    action.execute()
    context.action = action
    context.ring_visibility = _compute_ring_visibility(context.ordered_nodes)


@then('each node accepts traffic from its majority neighbors')
def step_check_visible(context):
    for node, (visible, _) in context.ring_visibility.items():
        for sender in visible:
            if sender != node:
                # sender can send UDP to node (node accepts from sender)
                assert_can_send_to(sender, node)


@then('each node blocks traffic from its blocked neighbors')
def step_check_blocked(context):
    for node, (_, blocked) in context.ring_visibility.items():
        for sender in blocked:
            # sender cannot send UDP to node (node blocks INPUT from sender)
            assert_cannot_send_to(sender, node)


@when('I heal the partition_majorities_ring action')
def step_heal_ring(context):
    context.action.heal()


# ---- Serialization ----

@given('a partition_majorities_ring action with ordinal {ordinal:d} and order "{nodes_csv}"')
def step_given_ring_action(context, ordinal, nodes_csv):
    context.action = PartitionMajoritiesRingAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal,
        ordered=nodes_csv.split(','),
    )


@when('I serialize and deserialize the partition_majorities_ring action')
def step_serialize_ring(context):
    serialized = context.action.serialize()
    context.deserialized = PartitionMajoritiesRingAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized action has the same ordered node list')
def step_check_deserialized_ring(context):
    orig = context.action
    deser = context.deserialized
    assert deser.ordinal == orig.ordinal, (
        f"Ordinal mismatch: {deser.ordinal} != {orig.ordinal}"
    )
    assert deser.ordered == orig.ordered, (
        f"Ordered mismatch: {deser.ordered} != {orig.ordered}"
    )
