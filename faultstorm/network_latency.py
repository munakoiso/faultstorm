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

The module also exposes a standalone :func:`freeze_drop_restore_for_ips`
function that **parses live** ``tc`` output to discover netem bands at
call time, so it can be used without holding a reference to the manager
instance.

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
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

from faultstorm.cluster import ClusterManager
from faultstorm.config import TestConfig

logger = logging.getLogger(__name__)

# Network interface inside containers.
_IFACE = "eth0"

# Huge delay used to "freeze" traffic before killing a process.
_FREEZE_DELAY_MS = 99999


def _normalise_dc_pair(a: str, b: str) -> Tuple[str, str]:
    """Return a canonical (sorted) DC pair key."""
    return (min(a, b), max(a, b))


# ------------------------------------------------------------------
# Band info stored during apply
# ------------------------------------------------------------------


@dataclass
class BandInfo:
    """Information about a single netem child qdisc band.

    Stored by :meth:`NetworkLatencyManager._apply_node` so that
    bands can later be manipulated without re-parsing ``tc`` output.
    """

    parent: str  # e.g. "1:2"
    handle: str  # e.g. "20:"
    delay_ms: int  # original delay in ms


# ------------------------------------------------------------------
# Targeted netem manipulation helpers
# ------------------------------------------------------------------


def _change_netem_delay(node: str, band: BandInfo, new_delay_ms: int) -> None:
    """Change the delay on an existing netem child qdisc.

    Args:
        node: Node name (Docker container).
        band: Band info (must have valid parent and handle).
        new_delay_ms: New delay value in milliseconds.
    """
    try:
        ClusterManager.exec_on_node(
            node,
            [
                "tc",
                "qdisc",
                "change",
                "dev",
                _IFACE,
                "parent",
                band.parent,
                "handle",
                band.handle,
                "netem",
                "delay",
                f"{new_delay_ms}ms",
            ],
            timeout=10,
        )
    except Exception as e:
        logger.warning(
            "tc qdisc change on %s parent=%s delay=%dms failed: %s",
            node,
            band.parent,
            new_delay_ms,
            e,
        )


def _reset_netem_band(node: str, band: BandInfo, restore_delay_ms: int) -> None:
    """Delete and re-create a netem child qdisc to drop queued packets.

    This drops all packets currently buffered in the netem queue for
    this band, then restores the band with the given delay.
    Filters attached to the parent prio qdisc are unaffected.

    Args:
        node: Node name (Docker container).
        band: Band info (parent and handle).
        restore_delay_ms: Delay to set on the re-created qdisc.
    """
    # Delete the child qdisc — drops all queued packets
    try:
        ClusterManager.exec_on_node(
            node,
            [
                "tc",
                "qdisc",
                "del",
                "dev",
                _IFACE,
                "parent",
                band.parent,
                "handle",
                band.handle,
            ],
            timeout=10,
        )
    except Exception as e:
        logger.warning(
            "tc qdisc del on %s parent=%s failed: %s",
            node,
            band.parent,
            e,
        )

    # Re-create with the desired delay
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
                band.parent,
                "handle",
                band.handle,
                "netem",
                "delay",
                f"{restore_delay_ms}ms",
            ],
            timeout=10,
        )
    except Exception as e:
        logger.warning(
            "tc qdisc add on %s parent=%s delay=%dms failed: %s",
            node,
            band.parent,
            restore_delay_ms,
            e,
        )


# ------------------------------------------------------------------
# Live tc output parsing
# ------------------------------------------------------------------

# Matches netem qdisc lines like:
#   qdisc netem 20: parent 1:2 limit 1000 delay 50.0ms
_NETEM_RE = re.compile(
    r"qdisc\s+netem\s+(?P<handle>\S+)\s+"
    r"parent\s+(?P<parent>\S+)\s+"
    r".*?delay\s+(?P<delay>[\d.]+)(?P<unit>us|ms|s)\b"
)

# Matches u32 filter lines like:
#   filter ... match 0ac80003/ffffffff at 16 ... flowid 1:2
_FILTER_IP_RE = re.compile(
    r"match\s+(?P<hex>[0-9a-f]{8})/ffffffff\s+at\s+16" r".*?flowid\s+(?P<flowid>\S+)"
)


def _parse_netem_bands(tc_qdisc_output: str) -> Dict[str, BandInfo]:
    """Parse ``tc qdisc show`` output into ``{parent: BandInfo}``.

    Returns a dict keyed by parent (e.g. ``"1:2"``) with the netem
    handle and delay for each band.
    """
    bands: Dict[str, BandInfo] = {}
    for m in _NETEM_RE.finditer(tc_qdisc_output):
        delay_val = float(m.group("delay"))
        unit = m.group("unit")
        if unit == "us":
            delay_ms = int(delay_val / 1000)
        elif unit == "s":
            delay_ms = int(delay_val * 1000)
        else:
            delay_ms = int(delay_val)
        parent = m.group("parent")
        handle = m.group("handle")
        bands[parent] = BandInfo(parent=parent, handle=handle, delay_ms=delay_ms)
    return bands


def _hex_to_ip(hex_str: str) -> str:
    """Convert 8-char hex string to dotted-quad IP (network byte order)."""
    n = int(hex_str, 16)
    return f"{(n >> 24) & 0xff}.{(n >> 16) & 0xff}.{(n >> 8) & 0xff}.{n & 0xff}"


def _parse_filter_ip_to_band(tc_filter_output: str) -> Dict[str, str]:
    """Parse ``tc filter show`` output into ``{dest_ip: flowid}``.

    Only matches ``u32`` rules that classify by destination IP
    (match at offset 16 with a /32 mask).
    """
    mapping: Dict[str, str] = {}
    for m in _FILTER_IP_RE.finditer(tc_filter_output):
        ip = _hex_to_ip(m.group("hex"))
        mapping[ip] = m.group("flowid")
    return mapping


def _discover_bands_for_ips(
    node: str,
    target_ips: Set[str],
) -> Dict[str, BandInfo]:
    """Discover netem bands on *node* that route traffic to *target_ips*.

    Parses the live ``tc`` state on the container to find which netem
    band handles each target IP.

    Args:
        node: Node name (Docker container).
        target_ips: Set of destination IPs to look for.

    Returns:
        ``{dest_ip: BandInfo}`` for every *target_ip* that has a
        matching netem band.  IPs without a matching band are omitted.
    """
    # 1. Get qdisc listing
    try:
        qdisc_out = ClusterManager.exec_on_node(
            node,
            ["tc", "qdisc", "show", "dev", _IFACE],
            timeout=10,
        )
    except Exception as e:
        logger.warning("tc qdisc show on %s failed: %s", node, e)
        return {}

    bands = _parse_netem_bands(qdisc_out)
    if not bands:
        logger.debug("No netem bands found on %s", node)
        return {}

    # 2. Get filter listing
    try:
        filter_out = ClusterManager.exec_on_node(
            node,
            ["tc", "filter", "show", "dev", _IFACE],
            timeout=10,
        )
    except Exception as e:
        logger.warning("tc filter show on %s failed: %s", node, e)
        return {}

    ip_to_flowid = _parse_filter_ip_to_band(filter_out)

    # 3. Match target IPs to bands
    result: Dict[str, BandInfo] = {}
    for ip in target_ips:
        flowid = ip_to_flowid.get(ip)
        if flowid and flowid in bands:
            result[ip] = bands[flowid]
        else:
            logger.debug(
                "No netem band for IP %s on %s (flowid=%s)",
                ip,
                node,
                flowid,
            )
    return result


# ------------------------------------------------------------------
# Temporary freeze rules (when no existing tc rules are present)
# ------------------------------------------------------------------


def _create_temp_netem_freeze(node: str, target_ips: Set[str]) -> bool:
    """Create a temporary ``prio`` + ``netem`` setup that freezes traffic.

    Used when no existing ``tc`` rules are present on the node.
    Creates a root ``prio`` qdisc with two bands (band 1 = default
    no-delay, band 2 = huge netem delay) and adds ``u32`` filters for
    every *target_ip* pointing to band 2.

    Args:
        node: Node name (Docker container).
        target_ips: IPs to freeze.

    Returns:
        ``True`` if the temporary setup was created successfully,
        ``False`` on failure.
    """
    num_bands = 2
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
        logger.warning("temp freeze: tc prio on %s failed: %s", node, e)
        return False

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
                "1:2",
                "handle",
                "20:",
                "netem",
                "delay",
                f"{_FREEZE_DELAY_MS}ms",
            ],
            timeout=10,
        )
    except Exception as e:
        logger.warning("temp freeze: tc netem on %s failed: %s", node, e)
        # Clean up the root qdisc we just created
        _remove_temp_netem_freeze(node)
        return False

    for ip in target_ips:
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
                    "2",
                    "u32",
                    "match",
                    "ip",
                    "dst",
                    f"{ip}/32",
                    "flowid",
                    "1:2",
                ],
                timeout=10,
            )
        except Exception as e:
            logger.warning(
                "temp freeze: tc filter for %s on %s failed: %s",
                ip,
                node,
                e,
            )

    logger.info(
        "Temporary freeze rules created on %s for %d IPs",
        node,
        len(target_ips),
    )
    return True


def _remove_temp_netem_freeze(node: str) -> None:
    """Remove the temporary root qdisc (and all children/filters)."""
    try:
        ClusterManager.exec_on_node(
            node,
            ["tc", "qdisc", "del", "dev", _IFACE, "root"],
            timeout=10,
        )
        logger.info("Temporary freeze rules removed from %s", node)
    except Exception as e:
        logger.warning(
            "temp freeze: tc cleanup on %s failed: %s",
            node,
            e,
        )


# ------------------------------------------------------------------
# Module-level freeze / drop / restore (parsing-based)
# ------------------------------------------------------------------


def freeze_drop_restore_for_ips(
    node: str,
    target_ips: Set[str],
    pre_kill_sleep: float = 5.0,
    kill_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Freeze traffic to *target_ips*, kill, drop queued packets, restore.

    This is a **standalone** function that parses ``tc`` output at call
    time to discover netem bands.  It does **not** require a
    :class:`NetworkLatencyManager` reference.

    Full sequence:

    1. Parse ``tc qdisc show`` and ``tc filter show`` on *node* to find
       netem bands routing traffic to *target_ips*.
    2. Change matched bands to a huge delay (freeze new packets).
       If no existing netem rules are found, a temporary ``prio``/``netem``
       setup is created from scratch.
    3. Sleep *pre_kill_sleep* seconds.
    4. Call *kill_callback()* (e.g. kill postgres + wipe PGDATA).
    5. If bands existed: delete and re-create matched bands with their
       **original** delays (drops queued packets, restores normal operation).
       If temporary rules were created: remove the root qdisc entirely.

    Args:
        node: Node name (Docker container).
        target_ips: Destination IPs whose bands should be frozen.
        pre_kill_sleep: Seconds to sleep between freeze and kill.
        kill_callback: Callable to invoke after sleep (step 4).
            Receives no arguments.  If ``None``, only freeze+drop+restore
            is performed (useful for testing).
    """
    matched = _discover_bands_for_ips(node, target_ips)

    if matched:
        # --- Path A: existing netem bands found → freeze / drop / restore ---

        # 1. Freeze: set huge delay on matched bands
        # Multiple IPs may share the same band — deduplicate by parent
        frozen_bands: Dict[str, BandInfo] = {}
        for ip, band in matched.items():
            if band.parent not in frozen_bands:
                frozen_bands[band.parent] = band
                logger.info(
                    "Freezing band %s (handle %s, original delay %dms) on %s",
                    band.parent,
                    band.handle,
                    band.delay_ms,
                    node,
                )
                _change_netem_delay(node, band, _FREEZE_DELAY_MS)

        # 2. Sleep
        logger.debug("Sleeping %.1fs before kill", pre_kill_sleep)
        time.sleep(pre_kill_sleep)

        # 3. Kill
        if kill_callback:
            kill_callback()

        # 4. Drop queued packets and restore original delays
        for parent, band in frozen_bands.items():
            logger.info(
                "Dropping queued packets and restoring band %s " "(handle %s, delay %dms) on %s",
                band.parent,
                band.handle,
                band.delay_ms,
                node,
            )
            _reset_netem_band(node, band, band.delay_ms)

    else:
        # --- Path B: no existing rules → create temporary freeze ---

        logger.info(
            "No existing netem bands for IPs %s on %s, " "creating temporary freeze rules",
            target_ips,
            node,
        )

        temp_created = _create_temp_netem_freeze(node, target_ips)

        # Sleep
        logger.debug("Sleeping %.1fs before kill", pre_kill_sleep)
        time.sleep(pre_kill_sleep)

        # Kill
        if kill_callback:
            kill_callback()

        # Remove temporary rules
        if temp_created:
            _remove_temp_netem_freeze(node)


class NetworkLatencyManager:
    """Applies and removes ``tc``/``netem`` latency rules on cluster nodes.

    The manager is stateful: :meth:`apply` records which nodes were
    configured so that :meth:`remove` can clean them up.

    For freeze/drop/restore operations, use the module-level
    :func:`freeze_drop_restore_for_ips` function which parses live
    ``tc`` output at call time and does not require a manager reference.
    """

    def __init__(self, config: TestConfig) -> None:
        self.config = config
        # Nodes that had tc rules applied (for cleanup).
        self._configured_nodes: Set[str] = set()
        # Per-node mapping: dest_ip → BandInfo
        self._node_bands: Dict[str, Dict[str, BandInfo]] = {}

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
        self._node_bands.clear()

    def get_bands_for_node(self, node: str) -> Dict[str, BandInfo]:
        """Return the stored ``{dest_ip: BandInfo}`` mapping for *node*.

        Returns an empty dict if no bands were recorded for this node
        (e.g. latency was not applied or has been removed).
        """
        return dict(self._node_bands.get(node, {}))

    def freeze_drop_restore_for_ips(
        self,
        node: str,
        target_ips: Set[str],
        pre_kill_sleep: float = 5.0,
        kill_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Freeze traffic to *target_ips*, kill, drop queued packets, restore.

        Full sequence:
        1. Look up stored band info for *node* and filter by *target_ips*.
        2. Change matched bands to a huge delay (freeze new packets).
        3. Sleep *pre_kill_sleep* seconds.
        4. Call *kill_callback()* (e.g. kill postgres + wipe PGDATA).
        5. Delete and re-create matched bands with their original delays
           (drops all queued packets and restores normal operation).

        Args:
            node: Node name (Docker container).
            target_ips: Destination IPs whose bands should be frozen.
            pre_kill_sleep: Seconds to sleep between freeze and kill.
            kill_callback: Callable to invoke after sleep (step 4).
                Receives no arguments.  If None, only freeze+drop+restore
                is performed (useful for testing).
        """
        node_bands = self._node_bands.get(node, {})
        matched: Dict[str, BandInfo] = {
            ip: band for ip, band in node_bands.items() if ip in target_ips
        }

        if not matched:
            logger.warning(
                "freeze_drop_restore: no stored bands for IPs %s on %s, "
                "proceeding with kill only",
                target_ips,
                node,
            )
            if kill_callback:
                kill_callback()
            return

        # 1. Freeze: set huge delay on matched bands
        # Multiple IPs may share the same band — deduplicate by parent
        frozen_bands: Dict[str, BandInfo] = {}
        for ip, band in matched.items():
            if band.parent not in frozen_bands:
                frozen_bands[band.parent] = band
                logger.info(
                    "Freezing band %s (handle %s, original delay %dms) on %s",
                    band.parent,
                    band.handle,
                    band.delay_ms,
                    node,
                )
                _change_netem_delay(node, band, _FREEZE_DELAY_MS)

        # 2. Sleep
        logger.debug("Sleeping %.1fs before kill", pre_kill_sleep)
        time.sleep(pre_kill_sleep)

        # 3. Kill
        if kill_callback:
            kill_callback()

        # 4. Drop queued packets and restore original delays
        for parent, band in frozen_bands.items():
            logger.info(
                "Dropping queued packets and restoring band %s " "(handle %s, delay %dms) on %s",
                band.parent,
                band.handle,
                band.delay_ms,
                node,
            )
            _reset_netem_band(node, band, band.delay_ms)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_node(self, node: str, dest_delays: Dict[str, int]) -> None:
        """Set up ``tc`` rules on a single node.

        Creates a ``prio`` root qdisc with enough bands for every
        distinct delay value, attaches ``netem`` child qdiscs, and adds
        ``u32`` filters to classify traffic by destination IP.

        Also stores the band mapping in ``self._node_bands[node]`` so
        that bands can later be manipulated without re-parsing ``tc``
        output.

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

        # Prepare band mapping for this node
        ip_to_band: Dict[str, BandInfo] = {}

        # 2. Attach netem child qdiscs (bands 2, 3, …).
        for band_idx, (delay_ms, ips) in enumerate(delay_to_ips.items(), start=2):
            handle = band_idx * 10
            parent = f"1:{band_idx}"
            handle_str = f"{handle}:"
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
                        handle_str,
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

            band_info = BandInfo(parent=parent, handle=handle_str, delay_ms=delay_ms)

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
                ip_to_band[ip] = band_info

        self._node_bands[node] = ip_to_band

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
