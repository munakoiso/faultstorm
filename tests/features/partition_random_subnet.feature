Feature: PartitionRandomSubnetAction
  The partition_random_subnet action applies directional network
  filtering on a specific node for a chosen subnet group.

  Scenario: Partition subnet blocks input traffic from ZK nodes and healing restores it
    When I execute a partition_random_subnet action on "faultstorm_node1" direction "input" subnet "zk"
    Then node "faultstorm_node1" cannot receive traffic from extra nodes
    And node "faultstorm_node1" can send traffic to extra nodes
    And node "faultstorm_node1" can reach the load node
    And the load node can reach "faultstorm_node1"
    When I heal the partition_random_subnet action
    Then all nodes can reach each other

  Scenario: Partition subnet blocks output traffic to DB nodes and healing restores it
    When I execute a partition_random_subnet action on "faultstorm_node1" direction "output" subnet "db"
    Then node "faultstorm_node1" cannot send traffic to other db nodes
    And node "faultstorm_node1" can send traffic to the load node
    And other db nodes can send traffic to "faultstorm_node1"
    And the load node can send traffic to "faultstorm_node1"
    When I heal the partition_random_subnet action
    Then all nodes can reach each other

  Scenario: Partition subnet blocks both directions for all subnets and healing restores it
    When I execute a partition_random_subnet action on "faultstorm_node1" direction "both" subnet "all"
    Then node "faultstorm_node1" cannot send traffic to any blocked node
    And no blocked node can send traffic to "faultstorm_node1"
    And node "faultstorm_node1" can send traffic to the load node
    And the load node can send traffic to "faultstorm_node1"
    When I heal the partition_random_subnet action
    Then all nodes can reach each other

  Scenario: Partition random subnet serialization round-trip
    Given a partition_random_subnet action with ordinal 6, node "faultstorm_node1", direction "input", subnet "zk" and blocked "faultstorm_extra1,faultstorm_extra2"
    When I serialize and deserialize the partition_random_subnet action
    Then the deserialized action has the same subnet parameters
