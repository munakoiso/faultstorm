Feature: PartitionRandomNodeAction
  The partition_random_node action isolates a single node
  from all other nodes, including the load node.

  Scenario: Partition isolates a specific node and healing restores connectivity
    When I execute a partition_random_node action isolating "faultstorm_node2"
    Then node "faultstorm_node2" cannot reach any other node
    And no other node can reach "faultstorm_node2"
    And the load node cannot reach "faultstorm_node2"
    And node "faultstorm_node2" cannot reach the load node
    And nodes other than "faultstorm_node2" can reach each other
    When I heal the partition_random_node action
    Then all nodes can reach each other

  Scenario: Partition random node serialization round-trip
    Given a partition_random_node action with ordinal 2 and node "faultstorm_node3"
    When I serialize and deserialize the partition_random_node action
    Then the deserialized action has ordinal 2 and node "faultstorm_node3"
