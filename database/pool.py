"""Reusable SQLite connections.

Opening a connection to inazina.db parses the whole schema (~1k triggers, ~17 ms),
so connections are kept and reused. Callers start threads freely (the web server
uses a thread per request), so this is one shared pool rather than thread-local
storage. Connections are keyed by kind and by the file's identity: a replaced
database file (restore, tests) never reuses a connection to the old one.
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

MAX_IDLE_PER_KIND = 16
_idle = {}
_lock = threading.Lock()


def _identity(path):
    if str(path).startswith("postgresql:"):
        return "dsn", str(path)
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _reset(connection, readonly):
    if connection.in_transaction:
        connection.rollback()
    connection.set_authorizer(None)
    connection.row_factory = sqlite3.Row
    if not readonly:
        connection.execute("PRAGMA foreign_keys=ON")


def _release(key, connection, readonly):
    if key is None:
        connection.close()
        return
    try:
        _reset(connection, readonly)
    except sqlite3.Error:
        connection.close()
        return
    with _lock:
        idle = _idle.get(key)
        if idle is None:
            # A new file identity: connections to the replaced file are useless.
            for stale in [k for k in _idle if k[0] == key[0] and k[1] != key[1]]:
                for old in _idle.pop(stale):
                    old.close()
            idle = _idle[key] = []
        if len(idle) < MAX_IDLE_PER_KIND:
            idle.append(connection)
            return
    connection.close()


@contextmanager
def pooled(kind, path, opener, *, readonly):
    """Yield a connection of ``kind`` to ``path``; ``opener()`` makes a new one."""
    identity = _identity(path)
    key = None if identity is None else (kind, identity)
    connection = None
    if key is not None:
        with _lock:
            idle = _idle.get(key)
            if idle:
                connection = idle.pop()
    if connection is None:
        connection = opener()
    try:
        yield connection
    except BaseException:
        connection.close()
        raise
    _release(key, connection, readonly)
