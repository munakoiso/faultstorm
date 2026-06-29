Feature: FreezeProcessesAction
  The freeze action creates a flag file on a target node to activate
  the process_freezer daemon, which sends SIGSTOP/SIGCONT to matching
  processes. Healing removes the flag file.

  Background:
    Given the process freezer daemon is running on all db nodes

  Scenario: Freeze action creates flag file on a specific node
    When I execute a freeze action for processes "sleep" on node "faultstorm_node1"
    Then the freeze flag file exists on node "faultstorm_node1"
    And the freeze flag file contains "sleep" on node "faultstorm_node1"
    When I heal the freeze action

  Scenario: Freeze action picks a random node when none specified
    When I execute a freeze action for processes "sleep" with no target node
    Then the freeze flag file exists on exactly one db node
    When I heal the freeze action

  Scenario: Healing removes the flag file
    When I execute a freeze action for processes "sleep" on node "faultstorm_node1"
    And I heal the freeze action
    Then the freeze flag file does not exist on node "faultstorm_node1"

  Scenario: Freeze actually stops a process temporarily
    Given a background process "sleep 3600" is running on node "faultstorm_node1"
    When I execute a freeze action for processes "sleep" on node "faultstorm_node1"
    And I wait for 5 seconds
    Then the process "sleep" on node "faultstorm_node1" was frozen at least once
    When I heal the freeze action
    Then the process "sleep" is still running on node "faultstorm_node1"

  Scenario: Freeze action with multiple process patterns
    When I execute a freeze action for processes "sleep,cat" on node "faultstorm_node1"
    Then the freeze flag file contains "sleep" on node "faultstorm_node1"
    And the freeze flag file contains "cat" on node "faultstorm_node1"
    When I heal the freeze action

  Scenario: Freeze action serialization round-trip
    Given a freeze action with ordinal 5, node "faultstorm_node2" and processes "postgres,pgconsul"
    When I serialize and deserialize the freeze action
    Then the deserialized freeze action has ordinal 5, node "faultstorm_node2" and processes "postgres,pgconsul"
