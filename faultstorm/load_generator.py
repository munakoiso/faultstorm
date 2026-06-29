"""
Load generator for FaultStorm tests.

Generates write and read load against a database cluster,
logging all operations as JSON for later consistency checking.

Writes are parallelized: ``writers_per_node`` writer threads per DB node.
Each DB operation is wrapped in an application-side timeout
(``config.operation_timeout``); if the database does not respond within
that time the operation is logged as indeterminate (INFO).
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

    Writes are parallelized across database nodes — ``writers_per_node``
    writer threads per node.  Each operation is wrapped in an
    application-side timeout; if the database does not respond within
    ``config.operation_timeout`` seconds, the operation is logged as
    indeterminate (INFO).
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
        self._counter_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._node_index = 0
        self._node_lock = threading.Lock()
        self._stop_event = threading.Event()

    def _get_next_value(self) -> int:
        """Get next sequential value (thread-safe).

        Returns:
            Next integer value
        """
        with self._counter_lock:
            self._counter += 1
            return self._counter

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
        """Write a JSON event to the operations log (thread-safe).

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
        with self._log_lock:
            log_file.write(json.dumps(event) + '\n')
            log_file.flush()

    @staticmethod
    def _run_with_timeout(fn, timeout_sec: float):  # type: ignore[type-arg]
        """Run a function with an application-side timeout.

        Starts *fn* in a daemon thread and waits up to *timeout_sec*
        seconds for it to complete.  If the function does not finish
        in time, ``TimeoutError`` is raised and the background thread
        is abandoned (daemon, so it won't block process exit).


        Args:
            fn: Callable to execute
            timeout_sec: Maximum seconds to wait

        Returns:
            Return value of *fn*

        Raises:
            TimeoutError: If *fn* did not complete in time
            Exception: Any exception raised by *fn*
        """
        result_holder: dict = {}
        done_event = threading.Event()

        def wrapper():
            try:
                result_holder['result'] = fn()
            except Exception as exc:
                result_holder['error'] = exc
            finally:
                done_event.set()

        t = threading.Thread(target=wrapper, daemon=True)
        t.start()

        if not done_event.wait(timeout=timeout_sec):
            raise TimeoutError(f"Operation timed out after {timeout_sec}s")

        if 'error' in result_holder:
            raise result_holder['error']
        return result_holder.get('result')

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

    def add(self, value: int, log_file: IO,
            node: Optional[str] = None) -> bool:
        """Add value to set table with application-side timeout.

        Logs INVOKE before attempting, then OK/FAIL/INFO based on result.

        Args:
            value: Integer value to insert
            log_file: Operations log file
            node: Target node (if None, uses round-robin)

        Returns:
            True if write confirmed successful
        """
        if node is None:
            node = self._get_next_node()
        self._log_event(log_file, INVOKE, "add", value=value, node=node)
        try:
            self._run_with_timeout(
                lambda: self.db_client.add(node, value),
                self.config.operation_timeout,
            )
            self._log_event(log_file, OK, "add", value=value, node=node)
            return True
        except TimeoutError:
            self._log_event(log_file, INFO, "add", value=value,
                            node=node, error="timeout")
            return False
        except Exception as e:
            if self.db_client.is_definite_failure(e):
                self._log_event(log_file, FAIL, "add", value=value,
                                node=node, error=str(e))
            else:
                self._log_event(log_file, INFO, "add", value=value,
                                node=node, error=str(e))
            return False

    def read(self, log_file: IO) -> Optional[set]:
        """Read all values from set table with application-side timeout.

        Args:
            log_file: Operations log file

        Returns:
            Set of values if successful, None otherwise
        """
        node = self._get_next_node()
        self._log_event(log_file, INVOKE, "read", node=node)
        try:
            values = self._run_with_timeout(
                lambda: self.db_client.read(node),
                self.config.operation_timeout,
            )
            self._log_event(log_file, OK, "read",
                            value=sorted(values), node=node)
            return values
        except TimeoutError:
            self._log_event(log_file, FAIL, "read",
                            node=node, error="timeout")
            return None
        except Exception as e:
            self._log_event(log_file, FAIL, "read",
                            node=node, error=str(e))
            return None

    def _writer_loop(self, node: str, log_file: IO,
                     done: threading.Event) -> None:
        """Write loop for a single writer thread.

        Continuously writes sequential values to the given node until
        *done* is set.

        Args:
            node: Target DB node
            log_file: Operations log file
            done: Event signalling that the write phase is over
        """
        while not done.is_set():
            value = self._get_next_value()
            self.add(value, log_file, node=node)
            if done.wait(self.config.add_interval):
                break

    def run_write_phase(self, duration: int, log_file: IO) -> None:
        """Run parallel writers for the specified duration.

        Spawns ``writers_per_node`` writer threads per DB node.  Each
        thread writes values sequentially with ``add_interval`` delay,
        using a shared atomic counter to assign unique values.

        Args:
            duration: Duration in seconds
            log_file: Operations log file
        """
        write_done = threading.Event()

        nodes = self.db_client.get_db_nodes()
        threads = []
        for node in nodes:
            for i in range(self.config.writers_per_node):
                t = threading.Thread(
                    target=self._writer_loop,
                    args=(node, log_file, write_done),
                    name=f"writer-{node}-{i}",
                )
                t.start()
                threads.append(t)

        total = len(threads)
        logger.info(
            "Started %d parallel writers (%d per node, %d nodes)",
            total, self.config.writers_per_node, len(nodes),
        )

        # Block until duration expires or stop() is called externally
        self._stop_event.wait(duration)
        write_done.set()

        for t in threads:
            t.join()

        logger.info("All writers stopped")

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
