"""Step definitions for WaitAction tests."""

import threading
import time

from behave import given, when, then

from faultstorm.faults.actions import WaitAction


@when('I execute a wait action for {seconds:d} seconds')
def step_execute_wait(context, seconds):
    action = WaitAction(
        context.db_nodes, context.extra_nodes, ordinal=1, seconds=seconds
    )
    start = time.monotonic()
    action.execute()
    context.elapsed = time.monotonic() - start


@when('I execute a wait action for {seconds:d} seconds with a stop event fired after {delay:d} second')
def step_execute_wait_with_stop(context, seconds, delay):
    action = WaitAction(
        context.db_nodes, context.extra_nodes, ordinal=1, seconds=seconds
    )
    stop_event = threading.Event()

    def fire():
        time.sleep(delay)
        stop_event.set()

    t = threading.Thread(target=fire, daemon=True)
    t.start()

    start = time.monotonic()
    action.execute(stop_event=stop_event)
    context.elapsed = time.monotonic() - start
    t.join(timeout=5)


@then('the elapsed time is at least {seconds:d} seconds')
def step_elapsed_at_least(context, seconds):
    assert context.elapsed >= seconds, (
        f"Expected at least {seconds}s, got {context.elapsed:.2f}s"
    )


@then('the elapsed time is less than {seconds:d} seconds')
def step_elapsed_less_than(context, seconds):
    assert context.elapsed < seconds, (
        f"Expected less than {seconds}s, got {context.elapsed:.2f}s"
    )


@given('a wait action with ordinal {ordinal:d} and {seconds:d} seconds')
def step_given_wait_action(context, ordinal, seconds):
    context.action = WaitAction(
        context.db_nodes, context.extra_nodes, ordinal=ordinal, seconds=seconds
    )


@when('I serialize and deserialize the wait action')
def step_serialize_deserialize_wait(context):
    serialized = context.action.serialize()
    context.deserialized = WaitAction.deserialize(
        serialized, context.db_nodes, context.extra_nodes
    )


@then('the deserialized wait action has ordinal {ordinal:d} and {seconds:d} seconds')
def step_check_deserialized_wait(context, ordinal, seconds):
    action = context.deserialized
    assert action.ordinal == ordinal, (
        f"Expected ordinal {ordinal}, got {action.ordinal}"
    )
    assert action.seconds == seconds, (
        f"Expected seconds {seconds}, got {action.seconds}"
    )
