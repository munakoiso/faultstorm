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
        ClusterManager.exec_on_node(
            node, ["iptables", "-N", chain_name], timeout=10
        )
        ClusterManager.exec_on_node(
            node, ["iptables", "-A", "INPUT", "-j", chain_name], timeout=10
        )
    except Exception as e:
        logger.warning("create_chain node=%s chain=%s error: %s",
                       node, chain_name, e)


def add_rule_to_chain(node: str, chain_name: str,
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


def remove_chain(node: str, chain_name: str) -> None:
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
