Feature: FreezeProcessesGroupAction
  The group freeze action creates a flag file on all nodes of a
  randomly chosen group (db or extra).

  Background:
    Given the process freezer daemon is running on all nodes

  Scenario: Group freeze with db group creates flag files on all DB nodes
    When I execute a group freeze action for processes "sleep" on group "db"
    Then the freeze flag file exists on all db nodes
    And the freeze flag file does not exist on any extra node
    When I heal the group freeze action
    Then the freeze flag file does not exist on any db node

  Scenario: Group freeze with extra group creates flag files on all extra nodes
    When I execute a group freeze action for processes "sleep" on group "extra"
    Then the freeze flag file exists on all extra nodes
    And the freeze flag file does not exist on any db node
    When I heal the group freeze action
    Then the freeze flag file does not exist on any extra node

  Scenario: Group freeze picks a random group when none specified
    When I execute a group freeze action for processes "sleep" with no target group
    Then the freeze flag file exists on all nodes of exactly one group
    When I heal the group freeze action

  Scenario: Group freeze serialization round-trip
    Given a group freeze action with ordinal 7, group "extra" and processes "postgres,java"
    When I serialize and deserialize the group freeze action
    Then the deserialized group freeze action has ordinal 7, group "extra" and processes "postgres,java"
