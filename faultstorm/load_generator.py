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
import logging
import threading
import time
from typing import IO, Any, Callable, Optional

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
        # Track in-flight timeout threads so we can drain them before
        # closing the ops log or exiting.
        self._pending_threads: list[threading.Thread] = []
        self._pending_lock = threading.Lock()

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

    def _log_event(self, log_file: IO[str], event_type: str, action: str, **kwargs: object) -> None:
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
            log_file.write(json.dumps(event) + "\n")
            log_file.flush()

    def _run_with_timeout(self, fn: Callable[[], Any], timeout_sec: float) -> Any:
        """Run a function with an application-side timeout.

        Starts *fn* in a daemon thread and waits up to *timeout_sec*
        seconds for it to complete.  If the function does not finish
        in time, ``TimeoutError`` is raised and the thread is tracked
        in ``_pending_threads`` so that :meth:`drain_pending` can wait
        for it to finish before the ops log is closed.

        Callers must ensure that *fn* cannot block indefinitely (e.g.
        by setting ``statement_timeout`` on the database connection).

        Args:
            fn: Callable to execute
            timeout_sec: Maximum seconds to wait

        Returns:
            Return value of *fn*

        Raises:
            TimeoutError: If *fn* did not complete in time
            Exception: Any exception raised by *fn*
        """
        result_holder: dict[str, Any] = {}
        done_event = threading.Event()

        def wrapper() -> None:
            try:
                result_holder["result"] = fn()
            except Exception as exc:
                result_holder["error"] = exc
            finally:
                done_event.set()

        t = threading.Thread(target=wrapper, daemon=True)
        t.start()

        if not done_event.wait(timeout=timeout_sec):
            # Track the abandoned thread so drain_pending() can wait for it.
            with self._pending_lock:
                self._pending_threads.append(t)
            raise TimeoutError(f"Operation timed out after {timeout_sec}s")

        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")

    def drain_pending(self, timeout: float = 30.0) -> None:
        """Wait for all in-flight timeout threads to finish.

        After writers are stopped, some database operations may still
        be running in daemon threads that timed out from the caller's
        perspective but are still executing against the database.
        Waiting for them ensures their writes either commit (and appear
        in the ops log as INFO/indeterminate) or fail before we close
        the log and proceed to the read phase.

        Args:
            timeout: Maximum seconds to wait for each thread.
        """
        with self._pending_lock:
            threads = list(self._pending_threads)
            self._pending_threads.clear()

        if threads:
            logger.info("Draining %d pending timeout threads...", len(threads))

        for t in threads:
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning("Pending thread %s did not finish within %.1fs", t.name, timeout)

        if threads:
            logger.info("All pending threads drained")

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
        raise RuntimeError(f"Failed to set up test table on any node: {nodes}")

    def add(self, value: int, log_file: IO[str], node: Optional[str] = None) -> bool:
        """Add value to set table with application-side timeout.

        Logs INVOKE before attempting, then OK/FAIL/INFO based on result.
        On timeout the daemon thread is abandoned but **not** killed.
        The ``DatabaseClient`` implementation must guarantee that the
        underlying operation cannot block indefinitely (e.g. by setting
        ``statement_timeout`` on the database connection) so the
        abandoned thread eventually terminates on its own.

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
            self._log_event(log_file, INFO, "add", value=value, node=node, error="timeout")
            return False
        except Exception as e:
            if self.db_client.is_definite_failure(e):
                self._log_event(log_file, FAIL, "add", value=value, node=node, error=str(e))
            else:
                self._log_event(log_file, INFO, "add", value=value, node=node, error=str(e))
            return False

    def read(self, log_file: IO[str]) -> Optional[set[int]]:
        """Read all values from set table with application-side timeout.

        On timeout the daemon thread is abandoned but **not** killed.
        The ``DatabaseClient`` implementation must guarantee that the
        underlying operation cannot block indefinitely (e.g. by setting
        ``statement_timeout`` on the database connection) so the
        abandoned thread eventually terminates on its own.

        Args:
            log_file: Operations log file

        Returns:
            Set of values if successful, None otherwise
        """
        node = self._get_next_node()
        self._log_event(log_file, INVOKE, "read", node=node)
        try:
            values: set[int] = self._run_with_timeout(
                lambda: self.db_client.read(node),
                self.config.operation_timeout,
            )
            self._log_event(log_file, OK, "read", value=sorted(values), node=node)
            return values
        except TimeoutError:
            self._log_event(log_file, FAIL, "read", node=node, error="timeout")
            return None
        except Exception as e:
            self._log_event(log_file, FAIL, "read", node=node, error=str(e))
            return None

    def _writer_loop(self, node: str, log_file: IO[str]) -> None:
        """Write loop for a single writer thread.

        Continuously writes sequential values to the given node until
        ``_stop_event`` is set.

        Args:
            node: Target DB node
            log_file: Operations log file
        """
        while not self._stop_event.is_set():
            value = self._get_next_value()
            self.add(value, log_file, node=node)
            if self._stop_event.wait(self.config.add_interval):
                break

    def run_write_phase(self, duration: int, log_file: IO[str]) -> None:
        """Run parallel writers for the specified duration.

        Spawns ``writers_per_node`` writer threads per DB node.  Each
        thread writes values sequentially with ``add_interval`` delay,
        using a shared atomic counter to assign unique values.

        Args:
            duration: Duration in seconds
            log_file: Operations log file
        """
        nodes = self.db_client.get_db_nodes()
        threads: list[threading.Thread] = []
        for node in nodes:
            for i in range(self.config.writers_per_node):
                t = threading.Thread(
                    target=self._writer_loop,
                    args=(node, log_file),
                    name=f"writer-{node}-{i}",
                )
                t.start()
                threads.append(t)

        total = len(threads)
        logger.info(
            "Started %d parallel writers (%d per node, %d nodes)",
            total,
            self.config.writers_per_node,
            len(nodes),
        )

        # Block until duration expires or stop() is called externally
        self._stop_event.wait(duration)
        self._stop_event.set()

        for t in threads:
            t.join()

        # Wait for any in-flight DB operations that timed out but may
        # still be executing in daemon threads.
        self.drain_pending()

        # Reset the stop event so that subsequent phases (e.g. read)
        # can run normally on the same LoadGenerator instance.
        self._stop_event.clear()

        logger.info("All writers stopped")

    def run_read_phase(self, duration: int, log_file: IO[str]) -> None:
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
        """Signal the load generator to stop.

        Writer threads observe ``_stop_event`` and exit on their own;
        :meth:`run_write_phase` joins them before returning.  This
        method is safe to call from a signal handler.
        """
        self._stop_event.set()
