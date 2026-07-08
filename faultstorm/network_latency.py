"""
Network latency emulation for FaultStorm tests.

Applies static ``tc``/``netem`` delays to Docker containers so that
cross-DC and DB↔ZK traffic experiences configurable latency for the
entire duration of a test run.

Usage::

    from faultstorm.network_latency import NetworkLatencyManager

    manager = NetworkLatencyManager(config)
    manager.apply(dc_map)       # before the test
    ...
    manager.remove()            # after the test (cleanup)

Implementation detail
---------------------
For every node that needs at least one delay rule the manager sets up a
``prio`` root qdisc with *N + 1* bands (band 0 is the default no-delay
band).  Each distinct delay value gets its own band with a ``netem``
child qdisc, and per-IP ``u32`` filters classify outgoing traffic to the
correct band.

All ``tc`` commands are executed via ``docker exec`` through
:class:`~faultstorm.cluster.ClusterManager`.
"""

import logging
from typing import Dict, List, Set, Tuple

from faultstorm.cluster import ClusterManager
from faultstorm.config import TestConfig

logger = logging.getLogger(__name__)

# Network interface inside containers.
_IFACE = "eth0"


def _normalise_dc_pair(a: str, b: str) -> Tuple[str, str]:
    """Return a canonical (sorted) DC pair key."""
    return (min(a, b), max(a, b))


class NetworkLatencyManager:
    """Applies and removes ``tc``/``netem`` latency rules on cluster nodes.

    The manager is stateful: :meth:`apply` records which nodes were
    configured so that :meth:`remove` can clean them up.
    """

    def __init__(self, config: TestConfig) -> None:
        self.config = config
        # Nodes that had tc rules applied (for cleanup).
        self._configured_nodes: Set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply(self, dc_map: Dict[str, List[str]]) -> None:
        """Apply latency rules to all cluster nodes.

        Args:
            dc_map: Mapping of DC name → list of node names.
        """
        cross_dc = self.config.cross_dc_delays
        db_zk_ms = self.config.db_zk_delay_ms

        if not cross_dc and db_zk_ms <= 0:
            logger.info("No network latency configured, skipping")
            return

        db_set = set(self.config.db_nodes)
        extra_set = set(self.config.extra_nodes)

        # Build node → DC lookup
        node_dc: Dict[str, str] = {}
        for dc, nodes in dc_map.items():
            for node in nodes:
                node_dc[node] = dc

        # Normalise cross_dc keys
        delays: Dict[Tuple[str, str], int] = {}
        for (a, b), ms in cross_dc.items():
            delays[_normalise_dc_pair(a, b)] = ms

        all_nodes = self.config.db_nodes + self.config.extra_nodes

        for node in all_nodes:
            node_dc_name = node_dc.get(node)
            # Build {dest_ip: delay_ms} for this node
            dest_delays: Dict[str, int] = {}

            for other in all_nodes:
                if other == node:
                    continue

                other_dc = node_dc.get(other)
                delay = 0

                # Cross-DC component
                if node_dc_name and other_dc and node_dc_name != other_dc:
                    pair = _normalise_dc_pair(node_dc_name, other_dc)
                    delay += delays.get(pair, 0)

                # DB ↔ ZK component
                if db_zk_ms > 0:
                    is_db_to_zk = node in db_set and other in extra_set
                    is_zk_to_db = node in extra_set and other in db_set
                    if is_db_to_zk or is_zk_to_db:
                        delay += db_zk_ms

                if delay > 0:
                    ip = ClusterManager.get_node_ip(other)
                    dest_delays[ip] = delay

            if not dest_delays:
                continue

            self._apply_node(node, dest_delays)
            self._configured_nodes.add(node)

        if self._configured_nodes:
            logger.info(
                "Network latency applied on %d nodes: %s",
                len(self._configured_nodes),
                sorted(self._configured_nodes),
            )

    def remove(self, force_all_nodes: bool = False) -> None:
        """Remove all previously applied latency rules.

        Args:
            force_all_nodes: if set - function will remove additional latency settings
              from all config nodes
        """
        if not self._configured_nodes and not force_all_nodes:
            return

        if force_all_nodes:
            self._configured_nodes = set(self.config.db_nodes + self.config.extra_nodes)

        for node in list(self._configured_nodes):
            self._remove_node(node)

        logger.info(
            "Network latency removed from %d nodes",
            len(self._configured_nodes),
        )
        self._configured_nodes.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_node(node: str, dest_delays: Dict[str, int]) -> None:
        """Set up ``tc`` rules on a single node.

        Creates a ``prio`` root qdisc with enough bands for every
        distinct delay value, attaches ``netem`` child qdiscs, and adds
        ``u32`` filters to classify traffic by destination IP.

        Args:
            node: Node name (Docker container).
            dest_delays: Mapping of destination IP → delay in ms.
        """
        # Group IPs by delay value so each unique delay gets one band.
        delay_to_ips: Dict[int, List[str]] = {}
        for ip, ms in dest_delays.items():
            delay_to_ips.setdefault(ms, []).append(ip)

        num_bands = len(delay_to_ips) + 1  # band 1 = default (no delay)

        # 1. Root prio qdisc.  Priomap sends everything to band 0
        #    (1-indexed as 1:1) by default — no delay.
        priomap = " ".join(["0"] * 16)
        try:
            ClusterManager.exec_on_node(
                node,
                [
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    _IFACE,
                    "root",
                    "handle",
                    "1:",
                    "prio",
                    "bands",
                    str(num_bands),
                    "priomap",
                    *priomap.split(),
                ],
                timeout=10,
            )
        except Exception as e:
            logger.warning("tc prio on %s failed: %s", node, e)
            return

        # 2. Attach netem child qdiscs (bands 2, 3, …).
        for band_idx, (delay_ms, ips) in enumerate(delay_to_ips.items(), start=2):
            handle = band_idx * 10
            parent = f"1:{band_idx}"
            try:
                ClusterManager.exec_on_node(
                    node,
                    [
                        "tc",
                        "qdisc",
                        "add",
                        "dev",
                        _IFACE,
                        "parent",
                        parent,
                        "handle",
                        f"{handle}:",
                        "netem",
                        "delay",
                        f"{delay_ms}ms",
                    ],
                    timeout=10,
                )
            except Exception as e:
                logger.warning(
                    "tc netem on %s band=%d delay=%dms failed: %s",
                    node,
                    band_idx,
                    delay_ms,
                    e,
                )
                continue

            # 3. Per-IP u32 filters → route to this band.
            for ip in ips:
                try:
                    ClusterManager.exec_on_node(
                        node,
                        [
                            "tc",
                            "filter",
                            "add",
                            "dev",
                            _IFACE,
                            "protocol",
                            "ip",
                            "parent",
                            "1:0",
                            "prio",
                            str(band_idx),
                            "u32",
                            "match",
                            "ip",
                            "dst",
                            f"{ip}/32",
                            "flowid",
                            parent,
                        ],
                        timeout=10,
                    )
                except Exception as e:
                    logger.warning(
                        "tc filter on %s ip=%s failed: %s",
                        node,
                        ip,
                        e,
                    )

        logger.debug(
            "tc rules applied on %s: %s",
            node,
            {ms: ips for ms, ips in delay_to_ips.items()},
        )

    @staticmethod
    def _remove_node(node: str) -> None:
        """Remove all ``tc`` rules from a node by deleting the root qdisc."""
        try:
            ClusterManager.exec_on_node(
                node,
                ["tc", "qdisc", "del", "dev", _IFACE, "root"],
                timeout=10,
            )
        except Exception as e:
            logger.warning("tc cleanup on %s failed: %s", node, e)
