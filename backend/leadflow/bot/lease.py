"""A dedicated PostgreSQL session owns polling until shutdown or connection loss.

Requires direct PostgreSQL or a session pooler, never transaction pooling.
"""

import psycopg
from django.db import connections


class PollingLeaseError(RuntimeError):
    pass


class PollingLease:
    def __init__(self, bot_id):
        # Reserve the negative bigint advisory-lock namespace for Telegram bot IDs.
        self.key = -int(bot_id)
        self.connection = None
        self.backend_pid = None

    def acquire(self):
        if self.connection is not None:
            raise PollingLeaseError("Polling lease is already open")
        params = connections["default"].get_connection_params()
        params.pop("cursor_factory", None)
        params["autocommit"] = True
        params.setdefault("connect_timeout", 10)
        params.setdefault("keepalives", 1)
        params.setdefault("keepalives_idle", 10)
        params.setdefault("keepalives_interval", 5)
        params.setdefault("keepalives_count", 2)
        params.setdefault("tcp_user_timeout", 20000)
        try:
            self.connection = psycopg.connect(**params)
            self.connection.execute("SET statement_timeout = '10s'")
            owned, self.backend_pid = self.connection.execute(
                "SELECT pg_try_advisory_lock(%s), pg_backend_pid()", (self.key,)
            ).fetchone()
            if not owned:
                raise PollingLeaseError("Another process already owns polling for this bot")
        except psycopg.Error, PollingLeaseError:
            self.close()
            raise PollingLeaseError("Unable to acquire exclusive bot polling lease") from None

    def check(self):
        if self.connection is None or self.connection.closed:
            raise PollingLeaseError("Bot polling lease connection was lost")
        try:
            held = self.connection.execute(
                "SELECT pg_backend_pid() = %s AND EXISTS ("
                "SELECT 1 FROM pg_locks WHERE locktype = %s AND pid = pg_backend_pid() "
                "AND classid = %s AND objid = %s AND objsubid = 1 AND granted)",
                (
                    self.backend_pid,
                    "advisory",
                    (self.key >> 32) & 0xFFFFFFFF,
                    self.key & 0xFFFFFFFF,
                ),
            ).fetchone()[0]
        except psycopg.Error:
            raise PollingLeaseError("Bot polling lease connection failed") from None
        if not held:
            raise PollingLeaseError("Bot polling lease is no longer held")

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
