Feature: WaitAction
  The wait action pauses execution for a specified number of seconds.

  Scenario: Wait action pauses for the specified duration
    When I execute a wait action for 2 seconds
    Then the elapsed time is at least 2 seconds

  Scenario: Wait action is interruptible via stop event
    When I execute a wait action for 10 seconds with a stop event fired after 1 second
    Then the elapsed time is less than 3 seconds

  Scenario: Wait action serialization round-trip
    Given a wait action with ordinal 5 and 30 seconds
    When I serialize and deserialize the wait action
    Then the deserialized wait action has ordinal 5 and 30 seconds
