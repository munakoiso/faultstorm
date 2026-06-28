Feature: PartitionMajoritiesRingAction
  The partition_majorities_ring action creates a ring partition
  where each node blocks INPUT from non-majority neighbors.
  Visibility is directional: "node A sees B" means A does not
  block incoming traffic from B (but B may block traffic from A).

  Scenario: Ring partition blocks traffic from non-neighbors and healing restores connectivity
    Given an ordered node list "faultstorm_node1,faultstorm_node2,faultstorm_node3,faultstorm_extra1,faultstorm_extra2"
    When I execute a partition_majorities_ring action with this order
    Then each node accepts traffic from its majority neighbors
    And each node blocks traffic from its blocked neighbors
    When I heal the partition_majorities_ring action
    Then all nodes can reach each other

  Scenario: Majorities ring serialization round-trip
    Given a partition_majorities_ring action with ordinal 4 and order "faultstorm_node1,faultstorm_node2,faultstorm_node3"
    When I serialize and deserialize the partition_majorities_ring action
    Then the deserialized action has the same ordered node list
