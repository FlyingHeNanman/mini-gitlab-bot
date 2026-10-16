"""SQLite 任务状态机：幂等入队 + 崩溃恢复。

状态流转：pending -> running -> done | failed
"""
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    issue_iid  INTEGER NOT NULL,
    state      TEXT NOT NULL DEFAULT 'pending',
    branch     TEXT,
    mr_iid     INTEGER,
    attempts   INTEGER DEFAULT 0,
    error      TEXT,
    created_at REAL,
    updated_at REAL,
    UNIQUE(project, issue_iid)
);
"""


class TaskStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def insert_if_absent(self, project: str, issue_iid: int) -> tuple[bool, int | None]:
        """幂等入队：已存在则返回 (False, task_id)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tasks WHERE project=? AND issue_iid=?",
                (project, issue_iid)).fetchone()
            if row:
                return False, row[0]
            self._conn.execute(
                "INSERT INTO tasks (project, issue_iid, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (project, issue_iid, "pending", time.time(), time.time()))
            self._conn.commit()
            task_id = self._conn.execute(
                "SELECT id FROM tasks WHERE project=? AND issue_iid=?",
                (project, issue_iid)).fetchone()[0]
            return True, task_id

    def set_state(self, task_id: int, state: str, **extra):
        """更新状态与附加字段（如 branch / mr_iid / attempts / error）。"""
        with self._lock:
            if extra:
                cols = ", ".join(f"{k}=?" for k in extra)
                sql = f"UPDATE tasks SET {cols}, state=?, updated_at=? WHERE id=?"
                self._conn.execute(sql, list(extra.values()) + [state, time.time(), task_id])
            else:
                self._conn.execute(
                    "UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                    (state, time.time(), task_id))
            self._conn.commit()

    def get(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).description]
            return dict(zip(cols, row))

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM tasks LIMIT 1").description]
            return [dict(zip(cols, r)) for r in rows]
