Feature: PartitionRandomHalvesAction
  The partition_random_halves action splits all nodes into two groups
  and blocks network traffic between them.

  Scenario: Partition halves blocks connectivity and healing restores it
    Given nodes are split into group1 "faultstorm_node1,faultstorm_extra1" and group2 "faultstorm_node2,faultstorm_node3,faultstorm_extra2"
    When I execute a partition_random_halves action with these groups
    Then nodes in group1 cannot reach nodes in group2
    And nodes in group2 cannot reach nodes in group1
    And nodes within group1 can reach each other
    And nodes within group2 can reach each other
    When I heal the partition_random_halves action
    Then all nodes can reach each other

  Scenario: Partition halves serialization round-trip
    Given a partition_random_halves action with ordinal 7, group1 "faultstorm_node1,faultstorm_extra1" and group2 "faultstorm_node2,faultstorm_node3"
    When I serialize and deserialize the partition_random_halves action
    Then the deserialized action has the same groups
