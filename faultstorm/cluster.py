"""
Docker cluster management for FaultStorm tests.

Provides utilities to execute commands inside Docker containers
and resolve container IPs.
"""

import logging
import subprocess
from typing import List

logger = logging.getLogger(__name__)

# Default Docker container name template.
# Use {node} as placeholder for the node name.
# Override via ClusterManager.container_template.
DEFAULT_CONTAINER_TEMPLATE = "{node}"


class ClusterManager:
    """Manages Docker containers for a database cluster.

    The container name is derived from the node name using a configurable
    template. Override ``container_template`` to match your Docker setup.

    Example::

        # Default: container name == node name
        ClusterManager.exec_on_node("postgresql1", ["pg_isready"])

        # Custom template for docker-compose projects:
        ClusterManager.container_template = "myproject_{node}_1"
        ClusterManager.exec_on_node("postgresql1", ["pg_isready"])
        # → runs in container "myproject_postgresql1_1"
    """

    container_template: str = DEFAULT_CONTAINER_TEMPLATE
    network_name: str = ""

    @classmethod
    def _container_name(cls, node: str) -> str:
        """Derive Docker container name from node name.

        Args:
            node: Logical node name

        Returns:
            Docker container name
        """
        return cls.container_template.format(node=node)

    @classmethod
    def exec_on_node(cls, node: str, command: List[str],
                     timeout: int = 30) -> str:
        """Execute command on a node via docker exec.

        Args:
            node: Node name
            command: Command and arguments
            timeout: Timeout in seconds

        Returns:
            Command stdout

        Raises:
            subprocess.CalledProcessError: If command fails
            subprocess.TimeoutExpired: If timeout exceeded
        """
        container = cls._container_name(node)
        docker_cmd = ["docker", "exec", container] + command
        logger.debug("exec_on_node %s: %s", node, ' '.join(docker_cmd))
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            logger.debug("exec_on_node %s failed (rc=%d): %s",
                         node, e.returncode, e.stderr.strip())
            raise
        except subprocess.TimeoutExpired:
            logger.warning("exec_on_node %s timed out after %ds", node, timeout)
            raise

    @classmethod
    def get_node_ip(cls, node: str) -> str:
        """Get the IP address of a node's Docker container.

        Args:
            node: Node name

        Returns:
            IP address string

        Raises:
            RuntimeError: If IP cannot be determined
        """
        container = cls._container_name(node)
        if cls.network_name:
            fmt = '{{.NetworkSettings.Networks.' + cls.network_name + '.IPAddress}}'
        else:
            fmt = '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", fmt, container],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            ip = result.stdout.strip()
            if not ip:
                raise RuntimeError(
                    f"Empty IP for container {container}"
                )
            return ip
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Cannot get IP for {container}: {e.stderr.strip()}"
            ) from e
