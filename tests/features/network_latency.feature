Feature: NetworkLatencyManager
  The NetworkLatencyManager applies static tc/netem delays
  to Docker containers so that cross-DC and DB↔ZK traffic
  experiences configurable latency.

  Scenario: Cross-DC latency increases ping RTT between nodes in different DCs
    Given a network latency config with cross-DC delay 50ms between "dc1" and "dc2"
    When I apply network latency
    Then ping from "faultstorm_node1" to "faultstorm_node2" takes at least 50ms
    And ping from "faultstorm_node2" to "faultstorm_node1" takes at least 50ms
    And ping from "faultstorm_node1" to "faultstorm_extra1" takes less than 10ms
    When I remove network latency
    Then ping from "faultstorm_node1" to "faultstorm_node2" takes less than 10ms

  Scenario: DB-ZK latency increases ping RTT from db nodes to extra nodes
    Given a network latency config with db-zk delay 50ms
    When I apply network latency
    Then ping from "faultstorm_node1" to "faultstorm_extra1" takes at least 50ms
    And ping from "faultstorm_extra1" to "faultstorm_node1" takes at least 50ms
    And ping from "faultstorm_node1" to "faultstorm_node2" takes less than 10ms
    When I remove network latency
    Then ping from "faultstorm_node1" to "faultstorm_extra1" takes less than 10ms

  Scenario: Cross-DC and DB-ZK latency stack for cross-DC db-zk pairs
    Given a network latency config with cross-DC delay 30ms between "dc1" and "dc2" and db-zk delay 30ms
    When I apply network latency
    Then ping from "faultstorm_node1" to "faultstorm_extra2" takes at least 60ms
    And ping from "faultstorm_node1" to "faultstorm_node2" takes at least 30ms
    And ping from "faultstorm_node1" to "faultstorm_extra1" takes at least 30ms
    When I remove network latency
    Then ping from "faultstorm_node1" to "faultstorm_extra2" takes less than 10ms
