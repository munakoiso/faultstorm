"""
Behave environment for faultstorm action tests.

Manages Docker Compose lifecycle and configures ClusterManager
so that actions can reach the test containers.
"""

import os
import subprocess
import logging

from faultstorm.cluster import ClusterManager

logger = logging.getLogger(__name__)

COMPOSE_DIR = os.path.join(os.path.dirname(__file__))
COMPOSE_FILE = os.path.join(COMPOSE_DIR, "docker-compose.yml")

DB_NODES = ["faultstorm_node1", "faultstorm_node2", "faultstorm_node3"]
EXTRA_NODES = ["faultstorm_extra1", "faultstorm_extra2"]
LOAD_NODE = "faultstorm_loadnode"
ALL_NODES = DB_NODES + EXTRA_NODES + [LOAD_NODE]

DC_MAP = {
    "dc1": ["faultstorm_node1", "faultstorm_extra1"],
    "dc2": ["faultstorm_node2", "faultstorm_extra2"],
    "dc3": ["faultstorm_node3"],
}


def _ensure_path():
    """Ensure common Docker binary locations are in PATH."""
    extra_paths = ["/opt/homebrew/bin", "/usr/local/bin"]
    current = os.environ.get("PATH", "")
    for p in extra_paths:
        if p not in current:
            current = p + ":" + current
    os.environ["PATH"] = current


def _run_compose(args: list, check: bool = True) -> None:
    """Run a docker compose command."""
    cmd = ["docker", "compose", "-f", COMPOSE_FILE] + args
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=120)


def _copy_bundled_scripts() -> None:
    """Copy bundled faultstorm scripts into the Docker build context."""
    import shutil
    src = os.path.join(os.path.dirname(__file__), os.pardir,
                       "faultstorm", "scripts", "process_freezer.sh")
    dst = os.path.join(os.path.dirname(__file__), "docker", "process_freezer.sh")
    src = os.path.normpath(src)
    dst = os.path.normpath(dst)
    if os.path.isfile(src):
        shutil.copy2(src, dst)
        logger.info("Copied %s -> %s", src, dst)
    else:
        logger.warning("process_freezer.sh not found at %s", src)


def before_all(context):
    """Start Docker Compose environment and configure ClusterManager."""
    _ensure_path()
    _copy_bundled_scripts()

    # Container name equals the container_name in docker-compose.yml
    # which is "faultstorm_<service>", and we use those as node names directly.
    ClusterManager.container_template = "{node}"
    ClusterManager.network_name = ""

    _run_compose(["up", "-d", "--build", "--wait"])

    context.db_nodes = list(DB_NODES)
    context.extra_nodes = list(EXTRA_NODES)
    context.load_node = LOAD_NODE
    context.all_nodes = list(ALL_NODES)
    context.dc_map = dict(DC_MAP)


FREEZE_FLAG_FILE = "/tmp/.process_freezer.flag"
FREEZE_LOG_FILE = "/var/log/process_freezer.log"


def before_scenario(context, scenario):
    """Clean up iptables rules, freeze flags and processes before each scenario."""
    for node in ALL_NODES:
        # Remove freeze flag file (deactivate freezer)
        try:
            ClusterManager.exec_on_node(
                node, ["rm", "-f", FREEZE_FLAG_FILE], timeout=5
            )
        except Exception:
            pass
        # Truncate freeze log so each scenario starts fresh
        try:
            ClusterManager.exec_on_node(
                node, ["truncate", "-s", "0", FREEZE_LOG_FILE], timeout=5
            )
        except Exception:
            pass
        # Flush all iptables rules to ensure clean state
        try:
            ClusterManager.exec_on_node(
                node, ["iptables", "-F"], timeout=10
            )
        except Exception:
            pass
        # Remove any custom FSTORM chains
        try:
            output = ClusterManager.exec_on_node(
                node, ["iptables", "-L", "-n"], timeout=10
            )
            for line in output.splitlines():
                if line.startswith("Chain FSTORM_"):
                    chain_name = line.split()[1]
                    try:
                        ClusterManager.exec_on_node(
                            node, ["iptables", "-F", chain_name], timeout=10
                        )
                        # Try removing from INPUT
                        ClusterManager.exec_on_node(
                            node,
                            ["iptables", "-D", "INPUT", "-j", chain_name],
                            timeout=10
                        )
                    except Exception:
                        pass
                    try:
                        # Try removing from OUTPUT
                        ClusterManager.exec_on_node(
                            node,
                            ["iptables", "-D", "OUTPUT", "-j", chain_name],
                            timeout=10
                        )
                    except Exception:
                        pass
                    try:
                        ClusterManager.exec_on_node(
                            node, ["iptables", "-X", chain_name], timeout=10
                        )
                    except Exception:
                        pass
        except Exception:
            pass


def after_scenario(context, scenario):
    """Clean up iptables and processes after each scenario."""
    # Same cleanup as before_scenario
    before_scenario(context, scenario)


def after_all(context):
    """Tear down Docker Compose environment."""
    _run_compose(["down", "-v", "--remove-orphans"], check=False)
