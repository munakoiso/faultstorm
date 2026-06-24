"""
Network partitioners for FaultStorm tests.

Implements partition-halves, partition-ring, partition-isolated-node.
Uses custom iptables chains for precise rule management.
"""

import logging
import random
import time
from typing import List, Dict, Tuple

from faultstorm.cluster import ClusterManager

logger = logging.getLogger(__name__)


class Partitioners:
    """Network partition implementations using iptables with custom chains."""

    # Track active partitions: partition_id -> chain_name
    _active_partitions: Dict[int, str] = {}
    _next_partition_id = 0

    @classmethod
    def get_next_id(cls) -> int:
        cls._next_partition_id += 1
        return cls._next_partition_id

    @classmethod
    def reset(cls) -> None:
        cls._active_partitions = {}
        cls._next_partition_id = 0

    @staticmethod
    def _get_chain_name(partition_id: int) -> str:
        """Get iptables chain name for a partition."""
        return f"FSTORM_PART_{partition_id}"

    @staticmethod
    def _create_chain(node: str, chain_name: str) -> None:
        """Create a new iptables chain and link it to INPUT."""
        try:
            ClusterManager.exec_on_node(
                node, ["iptables", "-N", chain_name], timeout=10
            )
            ClusterManager.exec_on_node(
                node, ["iptables", "-A", "INPUT", "-j", chain_name], timeout=10
            )
        except Exception as e:
            logger.warning("create_chain node=%s chain=%s error: %s",
                           node, chain_name, e)

    @staticmethod
    def _add_rule_to_chain(node: str, chain_name: str,
                           blocked_ips: List[str]) -> None:
        """Add DROP rules to a chain (one rule per IP)."""
        for ip in blocked_ips:
            try:
                ClusterManager.exec_on_node(
                    node,
                    ["iptables", "-A", chain_name, "-s", ip, "-j", "DROP"],
                    timeout=10,
                )
            except Exception as e:
                logger.warning("add_rule node=%s chain=%s ip=%s error: %s",
                               node, chain_name, ip, e)

    @staticmethod
    def _remove_chain(node: str, chain_name: str) -> None:
        """Remove an iptables chain (flush, unlink, delete)."""
        try:
            ClusterManager.exec_on_node(
                node, ["iptables", "-F", chain_name], timeout=10
            )
            ClusterManager.exec_on_node(
                node, ["iptables", "-D", "INPUT", "-j", chain_name], timeout=10
            )
            ClusterManager.exec_on_node(
                node, ["iptables", "-X", chain_name], timeout=10
            )
        except Exception as e:
            logger.warning("remove_chain node=%s chain=%s error: %s",
                           node, chain_name, e)

    # ---- Deterministic partition methods ----

    @staticmethod
    def partition_halves(group1: List[str], group2: List[str]) -> int:
        """Split nodes into two specified groups.

        Args:
            group1: First group of nodes
            group2: Second group of nodes

        Returns:
            Partition ID for later healing
        """
        partition_id = Partitioners.get_next_id()
        chain_name = Partitioners._get_chain_name(partition_id)

        group2_ips = [ClusterManager.get_node_ip(n) for n in group2]
        for node in group1:
            Partitioners._create_chain(node, chain_name)
            Partitioners._add_rule_to_chain(node, chain_name, group2_ips)

        group1_ips = [ClusterManager.get_node_ip(n) for n in group1]
        for node in group2:
            Partitioners._create_chain(node, chain_name)
            Partitioners._add_rule_to_chain(node, chain_name, group1_ips)

        Partitioners._active_partitions[partition_id] = chain_name

        logger.info("partition-halves id=%d group1=%s group2=%s",
                     partition_id, group1, group2)
        return partition_id

    @staticmethod
    def partition_ring(ordered_nodes: List[str]) -> int:
        """Each node sees a majority, but different majority (ring topology).

        Args:
            ordered_nodes: Nodes in ring order

        Returns:
            Partition ID for later healing
        """
        n = len(ordered_nodes)
        majority = (n // 2) + 1

        partition_id = Partitioners.get_next_id()
        chain_name = Partitioners._get_chain_name(partition_id)

        for i, node in enumerate(ordered_nodes):
            visible = []
            for j in range(majority):
                visible.append(ordered_nodes[(i + j) % n])
            blocked = [nd for nd in ordered_nodes if nd not in visible]

            blocked_ips = [ClusterManager.get_node_ip(nd) for nd in blocked]
            Partitioners._create_chain(node, chain_name)
            Partitioners._add_rule_to_chain(node, chain_name, blocked_ips)

        Partitioners._active_partitions[partition_id] = chain_name

        logger.info("partition-ring id=%d nodes=%s", partition_id, ordered_nodes)
        return partition_id

    @staticmethod
    def partition_isolated_node(isolated: str,
                                all_nodes: List[str]) -> int:
        """Isolate a specific node from all others.

        Args:
            isolated: Node to isolate
            all_nodes: List of all nodes

        Returns:
            Partition ID for later healing
        """
        others = [n for n in all_nodes if n != isolated]

        partition_id = Partitioners.get_next_id()
        chain_name = Partitioners._get_chain_name(partition_id)

        blocked_ips = [ClusterManager.get_node_ip(n) for n in others]
        Partitioners._create_chain(isolated, chain_name)
        Partitioners._add_rule_to_chain(isolated, chain_name, blocked_ips)

        isolated_ip = ClusterManager.get_node_ip(isolated)
        for node in others:
            Partitioners._create_chain(node, chain_name)
            Partitioners._add_rule_to_chain(node, chain_name, [isolated_ip])

        Partitioners._active_partitions[partition_id] = chain_name

        logger.info("partition-node id=%d isolated=%s", partition_id, isolated)
        return partition_id

    # ---- Healing ----

    @staticmethod
    def heal_partition(partition_id: int, nodes: List[str]) -> None:
        """Heal a specific partition by ID.

        Args:
            partition_id: ID of partition to heal
            nodes: List of all nodes
        """
        if partition_id not in Partitioners._active_partitions:
            logger.warning("heal: unknown partition id=%d", partition_id)
            return

        chain_name = Partitioners._active_partitions.pop(partition_id)

        for node in nodes:
            Partitioners._remove_chain(node, chain_name)

        logger.info("healed partition id=%d chain=%s", partition_id, chain_name)

    @staticmethod
    def heal_all(nodes: List[str]) -> None:
        """Heal all active partitions.

        Args:
            nodes: List of all nodes
        """
        partition_ids = list(Partitioners._active_partitions.keys())
        for pid in partition_ids:
            Partitioners.heal_partition(pid, nodes)

        Partitioners._active_partitions.clear()
        logger.info("all partitions healed")
