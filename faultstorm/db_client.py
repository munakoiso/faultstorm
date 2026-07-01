"""
Abstract database client for FaultStorm tests.

Defines the interface that database-specific implementations must provide.
Users testing different databases (PostgreSQL, MySQL, etc.) should subclass
DatabaseClient and implement the abstract methods.
"""

import logging
from abc import ABC, abstractmethod
from typing import List, Set

logger = logging.getLogger(__name__)


class DatabaseClient(ABC):
    """Abstract base class for database clients.

    Subclass this to add support for a specific database.
    The client must support two operations on a "set" table:
      - add(value): Insert an integer value
      - read(): Read all values
    """

    @abstractmethod
    def get_db_nodes(self) -> List[str]:
        """Return the list of database node names.

        These names are used for round-robin write/read distribution
        and must match what the fault engine knows about.

        Returns:
            List of database node identifiers
        """

    @abstractmethod
    def setup(self, node: str) -> None:
        """Set up the test table on the given node.

        Called once before the test starts. Should create the test table
        (e.g. CREATE TABLE IF NOT EXISTS). May be called on multiple nodes
        until one succeeds (typically the primary/leader).

        Args:
            node: Node identifier to connect to

        Raises:
            Exception: If setup fails on this node
        """

    @abstractmethod
    def add(self, node: str, value: int) -> None:
        """Insert a value into the set table.

        Must be atomic — either the value is durably written or an exception
        is raised. The connection should be opened and closed within this call
        (no persistent connections) to avoid stale connection issues during
        network partitions.

        Args:
            node: Node identifier to connect to
            value: Integer value to insert

        Raises:
            Exception: On any failure (connection, write, etc.)
        """

    @abstractmethod
    def read(self, node: str) -> Set[int]:
        """Read all values from the set table.

        Must return a consistent snapshot of all values in the table.

        Args:
            node: Node identifier to connect to

        Returns:
            Set of all integer values in the table

        Raises:
            Exception: On any failure
        """

    @abstractmethod
    def is_definite_failure(self, exc: Exception) -> bool:
        """Determine if an exception represents a definite (non-indeterminate) failure.

        A definite failure means the write definitely did NOT happen.
        For example, a "read-only transaction" error in PostgreSQL means
        the server rejected the write — the data was NOT written.

        If this returns False, the failure is treated as indeterminate
        (the write may or may not have been committed).

        This distinction is critical for correct consistency checking:
        - Definite failures (FAIL) → value is NOT expected in final read
        - Indeterminate failures (INFO) → value MAY appear in final read (recovered)

        Args:
            exc: The exception that occurred

        Returns:
            True if the failure is definite (write did not happen),
            False if indeterminate (write might have happened)
        """
