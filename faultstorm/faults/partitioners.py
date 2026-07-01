"""
Low-level iptables helpers for network partition actions.

Provides utilities to create/remove custom iptables chains,
add DROP rules, and heal partitions. Used by partition action
classes in actions.py.
"""

import logging
from typing import List

from faultstorm.cluster import ClusterManager

logger = logging.getLogger(__name__)


def get_chain_name(partition_id: int) -> str:
    """Get iptables chain name for a partition."""
    return f"FSTORM_PART_{partition_id}"


def create_chain(node: str, chain_name: str) -> None:
    """Create a new iptables chain and link it to INPUT."""
    try:
        ClusterManager.exec_on_node(node, ["iptables", "-N", chain_name], timeout=10)
        ClusterManager.exec_on_node(node, ["iptables", "-A", "INPUT", "-j", chain_name], timeout=10)
    except Exception as e:
        logger.warning("create_chain node=%s chain=%s error: %s", node, chain_name, e)


def add_drop_rules_by_src(node: str, chain_name: str, blocked_ips: List[str]) -> None:
    """Add DROP rules to a chain (one rule per IP)."""
    for ip in blocked_ips:
        try:
            ClusterManager.exec_on_node(
                node,
                ["iptables", "-A", chain_name, "-s", ip, "-j", "DROP"],
                timeout=10,
            )
        except Exception as e:
            logger.warning("add_rule node=%s chain=%s ip=%s error: %s", node, chain_name, ip, e)


def remove_chain(node: str, chain_name: str) -> None:
    """Remove an iptables chain (flush, unlink, delete)."""
    try:
        ClusterManager.exec_on_node(node, ["iptables", "-F", chain_name], timeout=10)
        ClusterManager.exec_on_node(node, ["iptables", "-D", "INPUT", "-j", chain_name], timeout=10)
        ClusterManager.exec_on_node(node, ["iptables", "-X", chain_name], timeout=10)
    except Exception as e:
        logger.warning("remove_chain node=%s chain=%s error: %s", node, chain_name, e)


def heal_partition(partition_id: int, nodes: List[str]) -> None:
    """Heal a specific partition by removing its iptables chain from all nodes.

    Args:
        partition_id: ID of partition to heal
        nodes: List of all nodes
    """
    chain_name = get_chain_name(partition_id)

    for node in nodes:
        remove_chain(node, chain_name)

    logger.info("healed partition id=%d chain=%s", partition_id, chain_name)


# ---- Directional partition helpers ----


def get_directional_chain_name(partition_id: int, direction: str) -> str:
    """Get iptables chain name for a directional partition.

    Args:
        partition_id: Partition ID
        direction: 'INPUT' or 'OUTPUT'

    Returns:
        Chain name like ``FSTORM_PART_5_IN`` or ``FSTORM_PART_5_OUT``
    """
    suffix = "IN" if direction == "INPUT" else "OUT"
    return f"FSTORM_PART_{partition_id}_{suffix}"


def create_chain_for_direction(node: str, chain_name: str, direction: str) -> None:
    """Create a new iptables chain and link it to the specified direction.

    Args:
        node: Target node
        chain_name: Custom chain name
        direction: 'INPUT' or 'OUTPUT'
    """
    try:
        ClusterManager.exec_on_node(node, ["iptables", "-N", chain_name], timeout=10)
        ClusterManager.exec_on_node(
            node, ["iptables", "-A", direction, "-j", chain_name], timeout=10
        )
    except Exception as e:
        logger.warning(
            "create_chain_for_direction node=%s chain=%s dir=%s " "error: %s",
            node,
            chain_name,
            direction,
            e,
        )


def add_drop_rules_by_dest(node: str, chain_name: str, dest_ips: List[str]) -> None:
    """Add DROP rules matching destination IPs (for OUTPUT filtering).

    Args:
        node: Target node
        chain_name: Chain to add rules to
        dest_ips: Destination IPs to drop
    """
    for ip in dest_ips:
        try:
            ClusterManager.exec_on_node(
                node,
                ["iptables", "-A", chain_name, "-d", ip, "-j", "DROP"],
                timeout=10,
            )
        except Exception as e:
            logger.warning(
                "add_drop_by_dest node=%s chain=%s ip=%s error: %s", node, chain_name, ip, e
            )


def remove_chain_for_direction(node: str, chain_name: str, direction: str) -> None:
    """Remove an iptables chain from a specific direction.

    Args:
        node: Target node
        chain_name: Chain to remove
        direction: 'INPUT' or 'OUTPUT'
    """
    try:
        ClusterManager.exec_on_node(node, ["iptables", "-F", chain_name], timeout=10)
        ClusterManager.exec_on_node(
            node, ["iptables", "-D", direction, "-j", chain_name], timeout=10
        )
        ClusterManager.exec_on_node(node, ["iptables", "-X", chain_name], timeout=10)
    except Exception as e:
        logger.warning(
            "remove_chain_for_direction node=%s chain=%s dir=%s " "error: %s",
            node,
            chain_name,
            direction,
            e,
        )


def heal_directional_partition(partition_id: int, node: str, directions: List[str]) -> None:
    """Heal a directional partition by removing its chains from a single node.

    Args:
        partition_id: ID of partition to heal
        node: The node that had iptables rules applied
        directions: List of iptables directions ('INPUT', 'OUTPUT', or both)
    """
    for direction in directions:
        chain_name = get_directional_chain_name(partition_id, direction)
        remove_chain_for_direction(node, chain_name, direction)

    logger.info(
        "healed directional partition id=%d node=%s directions=%s", partition_id, node, directions
    )
