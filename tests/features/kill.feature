Feature: KillProcessAction
  The kill action terminates a process on a target node.

  Scenario: Kill action terminates a running process on a specific node
    Given a background process "sleep 3600" is running on node "faultstorm_node1"
    When I execute a kill action for process "sleep" on node "faultstorm_node1"
    Then the process "sleep" is not running on node "faultstorm_node1"

  Scenario: Kill action picks a random node when none specified
    Given a background process "sleep 3600" is running on all db nodes
    When I execute a kill action for process "sleep" with no target node
    Then the process "sleep" is not running on exactly one db node

  Scenario: Kill action serialization round-trip
    Given a kill action with ordinal 3, process "postgres" and node "faultstorm_node2"
    When I serialize and deserialize the kill action
    Then the deserialized kill action has ordinal 3, process "postgres" and node "faultstorm_node2"
