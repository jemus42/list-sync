import os
import sqlite3
import subprocess
import time

import pytest

from list_sync.database import _reconcile_orphaned_syncs, _sync_owner_alive

pytestmark = pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs Linux /proc")


@pytest.fixture
def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_owner_alive(dead_pid):
    now = time.time()
    assert _sync_owner_alive(None, now) is False
    assert _sync_owner_alive(dead_pid, now) is False
    assert _sync_owner_alive(os.getpid(), now) is True
    # Row predates this process: the PID was reused, so the original owner is gone
    assert _sync_owner_alive(os.getpid(), now - 3600) is False


def test_reconcile_closes_only_orphans(dead_pid):
    cursor = sqlite3.connect(":memory:").cursor()
    cursor.execute(
        "CREATE TABLE sync_history (id INTEGER PRIMARY KEY, in_progress INTEGER DEFAULT 1,"
        " start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, end_time TIMESTAMP, status TEXT, pid INTEGER)"
    )
    me = os.getpid()
    cursor.executemany("INSERT INTO sync_history (pid) VALUES (?)", [(me,), (dead_pid,), (None,)])
    cursor.execute("INSERT INTO sync_history (pid, start_time) VALUES (?, datetime('now', '-1 hour'))", (me,))
    cursor.execute(
        "INSERT INTO sync_history (pid, in_progress, end_time, status) VALUES (?, 0, CURRENT_TIMESTAMP, 'completed')",
        (dead_pid,),
    )

    _reconcile_orphaned_syncs(cursor)

    rows = dict(cursor.execute("SELECT id, status FROM sync_history").fetchall())
    assert rows == {1: None, 2: "interrupted", 3: "interrupted", 4: "interrupted", 5: "completed"}
