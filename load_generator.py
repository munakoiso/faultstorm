"""
Load generator for FaultStorm tests.

Generates write and read load against a database cluster,
logging all operations as JSON for later consistency checking.
"""

import json
import time
import logging
import threading
from typing import Optional, IO

from faultstorm.config import TestConfig
from faultstorm.db_client import DatabaseClient

logger = logging.getLogger(__name__)

# Operation types
INVOKE = "invoke"
OK = "ok"
FAIL = "fail"
INFO = "info"


class LoadGenerator:
    """Generates load on a database cluster and logs operations as JSON.

    Distributes writes across database nodes using round-robin.
    Each operation is logged with type (invoke/ok/fail/info), action,
    value, node, and timestamp.
    """

    def __init__(self, config: TestConfig, db_client: DatabaseClient):
        """Initialize load generator.

        Args:
            config: Test configuration
            db_client: Database client for executing operations
        """
        self.config = config
        self.db_client = db_client
        self._counter = 0
        self._node_index = 0
        self._node_lock = threading.Lock()
        self._stop_event = threading.Event()

    def _get_next_node(self) -> str:
        """Get next DB node using round-robin.

        Returns:
            Node name
        """
        nodes = self.db_client.get_db_nodes()
        with self._node_lock:
            node = nodes[self._node_index % len(nodes)]
            self._node_index += 1
        return node

    def _log_event(self, log_file: IO, event_type: str, action: str,
                   **kwargs: object) -> None:
        """Write a JSON event to the operations log.

        Args:
            log_file: File to write to
            event_type: One of invoke/ok/fail/info
            action: Operation name (add/read)
            **kwargs: Additional fields (value, node, error, etc.)
        """
        event = {
            "type": event_type,
            "action": action,
            "timestamp": time.time(),
            **kwargs,
        }
        log_file.write(json.dumps(event) + '\n')
        log_file.flush()

    def setup(self) -> None:
        """Set up the test table.

        Tries each DB node until one succeeds.
        """
        nodes = self.db_client.get_db_nodes()
        for node in nodes:
            try:
                self.db_client.setup(node)
                logger.info("Test table created on %s", node)
                return
            except Exception as e:
                logger.debug("Setup on %s failed: %s", node, e)
        raise RuntimeError(
            f"Failed to set up test table on any node: {nodes}"
        )

    def add(self, value: int, log_file: IO) -> bool:
        """Add value to set table.

        Logs INVOKE before attempting, then OK/FAIL/INFO based on result.

        Args:
            value: Integer value to insert
            log_file: Operations log file

        Returns:
            True if write confirmed successful
        """
        node = self._get_next_node()
        self._log_event(log_file, INVOKE, "add", value=value, node=node)
        try:
            self.db_client.add(node, value)
            self._log_event(log_file, OK, "add", value=value, node=node)
            return True
        except Exception as e:
            if self.db_client.is_definite_failure(e):
                self._log_event(log_file, FAIL, "add", value=value,
                                node=node, error=str(e))
            else:
                self._log_event(log_file, INFO, "add", value=value,
                                node=node, error=str(e))
            return False

    def read(self, log_file: IO) -> Optional[set]:
        """Read all values from set table.

        Args:
            log_file: Operations log file

        Returns:
            Set of values if successful, None otherwise
        """
        node = self._get_next_node()
        self._log_event(log_file, INVOKE, "read", node=node)
        try:
            values = self.db_client.read(node)
            self._log_event(log_file, OK, "read",
                            value=sorted(values), node=node)
            return values
        except Exception as e:
            self._log_event(log_file, FAIL, "read",
                            node=node, error=str(e))
            return None

    def run_write_phase(self, duration: int, log_file: IO) -> None:
        """Run the write phase for the specified duration.

        Writes values sequentially with add_interval delay.

        Args:
            duration: Duration in seconds
            log_file: Operations log file
        """
        start_time = time.time()
        while (time.time() - start_time) < duration and not self._stop_event.is_set():
            self._counter += 1
            self.add(self._counter, log_file)
            if self._stop_event.wait(self.config.add_interval):
                break

    def run_read_phase(self, duration: int, log_file: IO) -> None:
        """Run the read phase for the specified duration.

        Reads from all nodes with read_interval delay.

        Args:
            duration: Duration in seconds
            log_file: Operations log file
        """
        start_time = time.time()
        while (time.time() - start_time) < duration and not self._stop_event.is_set():
            self.read(log_file)
            if self._stop_event.wait(self.config.read_interval):
                break

    def stop(self) -> None:
        """Signal the load generator to stop."""
        self._stop_event.set()
