#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Application charm that connects to database charms.

This charm is meant to be used only for testing
high availability of the MySQL charm.
"""

import logging
import secrets
import string
import subprocess
from typing import Dict, Optional

from charms.data_platform_libs.v0.database_requires import DatabaseRequires
from ops.charm import ActionEvent, CharmBase
from ops.main import main
from ops.model import ActiveStatus, Relation, WaitingStatus
from tenacity import RetryError, Retrying, stop_after_delay, wait_fixed

from connector import MySQLConnector  # isort: skip

logger = logging.getLogger(__name__)

CONTINUOUS_WRITE_TABLE_NAME = "data"
DATABASE_NAME = "continuous_writes_database"
DATABASE_RELATION = "database"
PEER = "application-peers"
PROC_PID_KEY = "proc-pid"
RANDOM_VALUE_KEY = "inserted_value"
RANDOM_VALUE_TABLE_NAME = "random_data"


class MySQLTestApplication(CharmBase):
    """Application charm that continuously writes to MySQL."""

    def __init__(self, *args):
        super().__init__(*args)

        # Charm events
        self.framework.observe(self.on.start, self._on_start)

        self.framework.observe(
            self.on.clear_continuous_writes_action, self._on_clear_continuous_writes_action
        )
        self.framework.observe(
            self.on.start_continuous_writes_action, self._on_start_continuous_writes_action
        )
        self.framework.observe(
            self.on.stop_continuous_writes_action, self._on_stop_continuous_writes_action
        )

        self.framework.observe(self.on.get_inserted_data_action, self._get_inserted_data)

        # Database related events
        self.database = DatabaseRequires(self, "database", DATABASE_NAME)
        self.framework.observe(self.database.on.database_created, self._on_database_created)
        self.framework.observe(self.database.on.endpoints_changed, self._on_endpoints_changed)
        self.framework.observe(
            self.on[DATABASE_RELATION].relation_broken, self._on_relation_broken
        )

    # ==============
    # Properties
    # ==============

    @property
    def _peers(self) -> Optional[Relation]:
        """Retrieve the peer relation (`ops.model.Relation`)."""
        return self.model.get_relation(PEER)

    @property
    def app_peer_data(self) -> Dict:
        """Application peer relation data object."""
        if self._peers is None:
            return {}

        return self._peers.data[self.app]

    @property
    def _database_config(self):
        """Returns the database config to use to connect to the MySQL cluster."""
        data = list(self.database.fetch_relation_data().values())[0]
        username, password, endpoints = (
            data.get("username"),
            data.get("password"),
            data.get("endpoints"),
        )
        if None in [username, password, endpoints]:
            return {}

        [host, port] = endpoints.split(":")

        return {
            "user": username,
            "password": password,
            "host": host,
            "port": port,
            "database": DATABASE_NAME,
        }

    # ==============
    # Helpers
    # ==============

    def _start_continuous_writes(self, starting_number: int) -> None:
        """Start continuous writes to the MySQL cluster."""
        if not self._database_config:
            return

        self._stop_continuous_writes()

        # Run continuous writes in the background
        proc = subprocess.Popen(
            [
                "/usr/bin/python3",
                "src/continuous_writes.py",
                self._database_config["user"],
                self._database_config["password"],
                self._database_config["host"],
                self._database_config["port"],
                self._database_config["database"],
                CONTINUOUS_WRITE_TABLE_NAME,
                str(starting_number),
            ]
        )

        # Store the continuous writes process id in stored state to be able to stop it later
        self.app_peer_data[PROC_PID_KEY] = str(proc.pid)

    def _stop_continuous_writes(self) -> Optional[int]:
        """Stop continuous writes to the MySQL cluster and return the last written value."""
        if not self._database_config:
            return None

        if not self.app_peer_data.get(PROC_PID_KEY):
            return None

        # Send a SIGKILL to the process and wait for the process to exit
        proc = subprocess.Popen(["pkill", "--signal", "SIGKILL", "-f", "src/continuous_writes.py"])
        proc.communicate()

        del self.app_peer_data[PROC_PID_KEY]

        last_written_value = -1
        # Query and return the max value inserted in the database
        # (else -1 if unable to query)
        try:
            for attempt in Retrying(stop=stop_after_delay(60), wait=wait_fixed(5)):
                with attempt:
                    last_written_value = self._max_written_value()
        except RetryError as e:
            logger.exception("Unable to query the database", exc_info=e)
        return last_written_value

    def _max_written_value(self) -> int:
        """Return the count of rows in the continuous writes table."""
        if not self._database_config:
            return -1

        with MySQLConnector(self._database_config) as cursor:
            cursor.execute(
                f"SELECT MAX(number) FROM `{DATABASE_NAME}`.`{CONTINUOUS_WRITE_TABLE_NAME}`;"
            )
            return cursor.fetchone()[0]

    def _create_test_table(self, cursor) -> None:
        """Create a test table in the database."""
        cursor.execute(
            (
                f"CREATE TABLE IF NOT EXISTS `{DATABASE_NAME}`.`{RANDOM_VALUE_TABLE_NAME}`("
                "id SMALLINT NOT NULL AUTO_INCREMENT, "
                "data VARCHAR(255), "
                "PRIMARY KEY (id))"
            )
        )

    def _insert_test_data(self, cursor, random_value: str) -> None:
        """Insert the provided random value into the test table in the database."""
        cursor.execute(
            f"INSERT INTO `{DATABASE_NAME}`.`{RANDOM_VALUE_TABLE_NAME}`(data) VALUES({random_value})"
        )

    @staticmethod
    def _generate_random_values(length) -> str:
        choices = string.ascii_letters + string.digits
        return "".join(secrets.choice(choices) for _ in range(length))

    def _write_random_value(self) -> str:
        """Write a random value to the database."""
        if not self._database_config:
            return ""

        with MySQLConnector(self._database_config) as cursor:
            self._create_test_table(cursor)
            random_value = self._generate_random_values(10)
            self._insert_test_data(cursor, random_value)

        return random_value

    # ==============
    # Handlers
    # ==============
    def _on_start(self, _) -> None:
        """Handle the start event."""
        self.unit.set_workload_version("0.0.1")
        self.unit.status = WaitingStatus()

    def _on_clear_continuous_writes_action(self, _) -> None:
        """Handle the clear continuous writes action event."""
        if not self._database_config:
            return

        self._stop_continuous_writes()
        with MySQLConnector(self._database_config) as cursor:
            cursor.execute(
                f"DROP TABLE IF EXISTS `{DATABASE_NAME}`.`{CONTINUOUS_WRITE_TABLE_NAME}`;"
            )

    def _on_start_continuous_writes_action(self, _) -> None:
        """Handle the start continuous writes action event."""
        if not self._database_config:
            return

        self._start_continuous_writes(1)

    def _on_stop_continuous_writes_action(self, event: ActionEvent) -> None:
        """Handle the stop continuous writes action event."""
        if not self._database_config:
            return event.set_results({"writes": 0})

        writes = self._stop_continuous_writes()
        event.set_results({"writes": writes})

    def _on_database_created(self, _) -> None:
        """Handle the database created event."""
        self._start_continuous_writes(1)
        value = self._write_random_value()
        self.app_peer_data[RANDOM_VALUE_KEY] = value
        self.unit.status = ActiveStatus()

    def _on_endpoints_changed(self, _) -> None:
        """Handle the database endpoints changed event."""
        count = self._max_written_value()
        self._start_continuous_writes(count + 1)

    def _on_relation_broken(self, _) -> None:
        """Handle the database relation broken event."""
        self.unit.status = WaitingStatus()

    def _get_inserted_data(self, event: ActionEvent) -> None:
        """Get random value inserted into the database."""
        event.set_results({"data": self.app_peer_data.get(RANDOM_VALUE_KEY, "empty")})


if __name__ == "__main__":
    main(MySQLTestApplication)
