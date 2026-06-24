"""
Test runner for FaultStorm tests.

This is the main API for running fault-injection tests. It is database-agnostic
and works with any DatabaseClient implementation.
"""

import os
import logging
import threading
from datetime import datetime

from faultstorm.config import TestConfig
from faultstorm.db_client import DatabaseClient
from faultstorm.load_generator import LoadGenerator
from faultstorm.faults.engine import FaultEngine
from faultstorm.faults.actions import FaultRegistry
from faultstorm.faults.partitioners import Partitioners
from faultstorm.checker import check_consistency
from faultstorm.model import CheckResult

logger = logging.getLogger(__name__)


class TestRunner:
    """Main API for running fault-injection tests.

    Coordinates the load generator, fault engine, and checker
    to execute a complete test cycle: setup → write+faults → read → check.
    """

    def __init__(self, config: TestConfig, db_client: DatabaseClient,
                 fault_registry: FaultRegistry):
        """Initialize test runner.

        Args:
            config: Test configuration
            db_client: Database client implementing DatabaseClient interface
            fault_registry: Registry of fault action classes
        """
        self.config = config
        self.db_client = db_client
        self.fault_registry = fault_registry

    def run(self) -> CheckResult:
        """Run a complete fault-injection test.

        Returns:
            CheckResult with validation results
        """
        # Create log directories
        for log_path in (self.config.operations_log, self.config.scenario_log):
            log_dir = os.path.dirname(log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)

        # Reset partitioners state
        Partitioners.reset()

        logger.info("Starting test: %s", self.config.name)
        logger.info("Write phase: %d seconds", self.config.write_phase_duration)
        logger.info("Read phase: %d seconds", self.config.read_phase_duration)
        if self.config.replay_scenario:
            logger.info("Replay mode: %s", self.config.replay_scenario)
        else:
            logger.info("Random fault mode, scenario log: %s",
                        self.config.scenario_log)

        with open(self.config.operations_log, 'w') as ops_log:

            load_gen = LoadGenerator(self.config, self.db_client)
            engine = FaultEngine(self.config, self.fault_registry)

            # Setup
            logger.info("Setting up test table...")
            load_gen.setup()

            # Phase 1: Write + Faults
            logger.info("Phase 1: Write + Faults started at %s",
                        datetime.now().isoformat())

            if self.config.replay_scenario:
                fault_thread = threading.Thread(
                    target=engine.run_replay,
                    args=(self.config.replay_scenario, self.config.scenario_log)
                )
            else:
                fault_thread = threading.Thread(
                    target=engine.run_random,
                    args=(self.config.write_phase_duration, self.config.scenario_log)
                )
            fault_thread.start()

            load_gen.run_write_phase(self.config.write_phase_duration, ops_log)

            engine.stop()
            fault_thread.join()
            engine.heal_all()

            logger.info("Phase 1 completed")

            # Phase 2: Read validation
            logger.info("Phase 2: Read validation started at %s",
                        datetime.now().isoformat())

            load_gen.run_read_phase(self.config.read_phase_duration, ops_log)
            load_gen.stop()

            logger.info("Phase 2 completed")

        # Check results
        logger.info("Checking consistency...")
        result = check_consistency(self.config.operations_log)

        return result

    def run_and_print(self) -> bool:
        """Run test and print results to stdout.

        Returns:
            True if test passed
        """
        result = self.run()

        print("\n" + "=" * 60)
        print("Test Results")
        print("=" * 60)
        print(f"Valid: {result.valid}")
        print(f"Total attempts: {result.total_attempts}")
        print(f"Successful adds: {result.successful_adds}")
        print(f"Failed adds: {result.failed_adds}")
        print(f"Write availability: {result.write_availability:.2%}")

        if result.recovered:
            print(f"Recovered values (indeterminate writes that went through): "
                  f"{len(result.recovered)}")
            if len(result.recovered) < 20:
                print(f"  {sorted(result.recovered)}")

        if result.lost:
            print(f"LOST values (DATA LOSS): {len(result.lost)}")
            if len(result.lost) < 20:
                print(f"  {sorted(result.lost)}")

        if result.unexpected:
            print(f"UNEXPECTED values (CORRUPTION): {len(result.unexpected)}")
            if len(result.unexpected) < 20:
                print(f"  {sorted(result.unexpected)}")

        if result.errors:
            print(f"Errors: {result.errors}")

        print(f"Scenario log: {self.config.scenario_log}")
        print("=" * 60)

        if not result.valid:
            print("\nScenario Log:")
            print("-" * 60)
            try:
                with open(self.config.scenario_log, 'r') as f:
                    print(f.read())
            except FileNotFoundError:
                print("(scenario log not found)")

        return result.valid
