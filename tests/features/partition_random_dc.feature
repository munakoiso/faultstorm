Feature: PartitionRandomDcAction
  The partition_random_dc action isolates all nodes of a datacenter
  from the rest of the cluster, including the load node.

  Scenario: Partition DC isolates datacenter and healing restores connectivity
    When I execute a partition_random_dc action isolating DC "dc1"
    Then nodes in DC "dc1" cannot reach nodes outside DC "dc1"
    And nodes outside DC "dc1" cannot reach nodes in DC "dc1"
    And nodes within DC "dc1" can reach each other
    And the load node cannot reach nodes in DC "dc1"
    And nodes in DC "dc1" cannot reach the load node
    When I heal the partition_random_dc action
    Then all nodes can reach each other

  Scenario: Partition DC with empty dc_map is a no-op
    When I execute a partition_random_dc action with empty dc_map
    Then all nodes can reach each other

  Scenario: Partition random DC serialization round-trip
    Given a partition_random_dc action with ordinal 8 and dc_name "dc2"
    When I serialize and deserialize the partition_random_dc action
    Then the deserialized action has ordinal 8 and dc_name "dc2"
