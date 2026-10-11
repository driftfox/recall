# opencode Session Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opencode as a sixth indexable source in recall, reading sessions from opencode's SQLite database and making them searchable and resumable.

**Architecture:** opencode stores all sessions/messages/parts in one SQLite file, not per-session JSONL files. A cross-platform resolver locates that file, a parser reads user/assistant `text` parts into the same `(metadata, messages)` shape the existing parsers return, and a dedicated `index_opencode()` step upserts sessions into the index by `session_id` (dedup on `time_updated`). `read_session.py` learns to read a `<db>#<session-id>` reference.

**Tech Stack:** Python 3.9+ stdlib only (`sqlite3`, `json`, `pathlib`, `unittest`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-10-opencode-support-design.md`

## Global Constraints

- Python 3.9+ stdlib only. No new third-party dependencies.
- Must work on Linux, macOS and Windows (recall runs on all three).
- Never write to or lock opencode's database — open it read-only.
- The existing file-scan pipeline must stay unchanged; opencode is isolated in its own function called from `index_sessions()`.
- Follow the Cursor parser's conventions: skip reasoning/tools, same metadata dict keys, `--source`, docs and synthetic-fixture tests.
- All work happens on branch `opencode-support` (already checked out, based on `feat/cursor-agent-support`).

## File Structure

- `scripts/recall.py` — add: opencode dir/DB resolver, `parse_opencode_sessions()`, `index_opencode()`, wire into `index_sessions()`, add `opencode` to argparse `choices`.
- `scripts/read_session.py` — add opencode detection and a DB-reading branch.
- `tests/test_opencode_parser.py` — new; resolver, parser, indexing, reader tests.
- `tests/test_ranking.py`, `tests/test_role_column.py` — point `OPENCODE_DB` at an empty path so they don't index the host's real DB.
- `SKILL.md`, `README.md`, `CHANGELOG.md` — docs.

## Review Focus

Uncovered input classes/failure modes most likely to bite a user, each pinned to a test in its owning task:

1. A **live opencode DB in WAL mode** (with `-wal`/`-shm` siblings) opened read-only while opencode is running — must read without error or lock. (Task 2)
2. A path containing **spaces** (macOS `Application Support`, Windows `AppData\Local`) in both the DB path and the `db#id` reference — URI encoding and splitting on the last `#`. (Tasks 2, 4)
3. An **`OPENCODE_DB` override** or non-stable **channel filename** (`opencode-<channel>.db`) — resolution must honor them. (Task 1)
4. A session with **no text parts**, an empty title, or a missing directory — must index (or skip) without crashing; slug falls back to the id. (Task 2)
5. **Malformed JSON** in a `data` blob — must skip that row, not abort the whole index. (Task 2)

---

### Task 1: Cross-platform opencode database resolution

**Files:**
- Modify: `scripts/recall.py` (constants block near line 34; new functions near the Cursor parser, above `index_sessions`)
- Test: `tests/test_opencode_parser.py` (create)

**Interfaces:**
- Produces:
  - `opencode_data_dir() -> Path` — the per-OS `opencode` data directory.
  - `resolve_opencode_db() -> Optional[Path]` — the DB file, or `None` when `OPENCODE_DB=:memory:`.
  - module-level `OPENCODE_DB: Optional[Path] = resolve_opencode_db()` (tests override this).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_opencode_parser.py`:

```python
"""Tests for opencode support: DB resolution, parser, indexing, and reader."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import recall  # noqa: E402
import read_session  # noqa: E402


def _no_opencode_env(extra=None):
    """Context manager: patch os.environ, always clearing OPENCODE_DB."""
    env = dict(extra or {})
    env.pop("OPENCODE_DB", None)
    return mock.patch.dict(os.environ, env, clear=False)


class TestOpencodeDataDir(unittest.TestCase):
    def test_linux_uses_xdg_data_home(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.opencode_data_dir(), Path("/xdg") / "opencode")

    def test_macos_uses_application_support(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                recall.opencode_data_dir(),
                Path.home() / "Library" / "Application Support" / "opencode",
            )

    def test_windows_uses_localappdata_data(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\a\AppData\Local"}):
            self.assertEqual(
                recall.opencode_data_dir(),
                Path(r"C:\Users\a\AppData\Local") / "opencode" / "Data",
            )


class TestResolveOpencodeDb(unittest.TestCase):
    def test_absolute_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": "/tmp/custom.db"}):
            self.assertEqual(recall.resolve_opencode_db(), Path("/tmp/custom.db"))

    def test_bare_filename_resolves_inside_data_dir(self):
        with mock.patch.object(sys, "platform", "linux"), \
             mock.patch.dict(os.environ, {"OPENCODE_DB": "custom.db", "XDG_DATA_HOME": "/xdg"}):
            self.assertEqual(recall.resolve_opencode_db(), Path("/xdg/opencode/custom.db"))

    def test_memory_override_is_none(self):
        with mock.patch.dict(os.environ, {"OPENCODE_DB": ":memory:"}):
            self.assertIsNone(recall.resolve_opencode_db())

    def test_prefers_opencode_db(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "opencode").mkdir()
            (Path(d) / "opencode" / "opencode.db").write_text("")
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )

    def test_falls_back_to_newest_channel_db(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "opencode"
            base.mkdir()
            older = base / "opencode-beta.db"
            newer = base / "opencode-dev.db"
            older.write_text("")
            newer.write_text("")
            os.utime(older, (1_000, 1_000))
            os.utime(newer, (2_000, 2_000))
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(recall.resolve_opencode_db(), newer)

    def test_missing_db_returns_default_path(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(sys, "platform", "linux"), _no_opencode_env({"XDG_DATA_HOME": d}):
                self.assertEqual(
                    recall.resolve_opencode_db(), Path(d) / "opencode" / "opencode.db"
                )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_opencode_parser.TestOpencodeDataDir -v` (from the repo root)
Expected: FAIL with `AttributeError: module 'recall' has no attribute 'opencode_data_dir'`.

- [ ] **Step 3: Implement the resolver in `scripts/recall.py`**

Add the functions just above the Cursor parser section (below the other parsers):

```python
def opencode_data_dir():
    """Return opencode's data directory for the current OS.

    Mirrors opencode's own env-paths lookup: XDG data dir on Linux, Application
    Support on macOS, LOCALAPPDATA on Windows.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "opencode"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "opencode" / "Data"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "opencode"


def resolve_opencode_db():
    """Return opencode's database path, or None when there is no usable DB.

    Honors the OPENCODE_DB override (absolute, or a filename inside the data
    dir; ``:memory:`` means none). Otherwise prefers ``opencode.db`` (stable
    channel) and falls back to the newest ``opencode-*.db`` (other channels).
    """
    override = os.environ.get("OPENCODE_DB")
    data_dir = opencode_data_dir()
    if override:
        if override == ":memory:":
            return None
        path = Path(override)
        return path if path.is_absolute() else data_dir / path

    default = data_dir / "opencode.db"
    if default.exists() or not data_dir.is_dir():
        return default
    channel_dbs = list(data_dir.glob("opencode-*.db"))
    if channel_dbs:
        return max(channel_dbs, key=lambda p: p.stat().st_mtime)
    return default
```

Note: define the module-level `OPENCODE_DB` **after** the two functions, at module scope (below the definitions), so it calls `resolve_opencode_db()` at import:

```python
OPENCODE_DB = resolve_opencode_db()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_opencode_parser -v`
Expected: PASS (resolver tests; parser/index/reader classes not added yet).

- [ ] **Step 5: Commit**

```bash
git add scripts/recall.py tests/test_opencode_parser.py
git commit -m "feat(opencode): resolve opencode database path cross-platform"
```

---

### Task 2: Parse sessions out of the opencode database

**Files:**
- Modify: `scripts/recall.py` (new parser below the resolver)
- Test: `tests/test_opencode_parser.py` (append)

**Interfaces:**
- Consumes: `OPENCODE_DB` / `resolve_opencode_db()` from Task 1.
- Produces:
  - `parse_opencode_sessions(db_path) -> Iterator[tuple[dict, list[tuple[str, str]]]]` — yields `(metadata, messages)` per session, where `metadata` has keys `session_id, source, file_path, project, slug, timestamp, mtime` (same as the other parsers, plus `mtime`).
  - `_open_opencode_db(db_path) -> sqlite3.Connection` — read-only connection with a plain-connection fallback.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opencode_parser.py`:

```python
def make_opencode_db(path, sessions):
    """Create a minimal opencode-shaped SQLite DB.

    ``sessions`` is a list of dicts:
      {"id", "title", "directory", "created", "updated",
       "messages": [{"id", "role", "parts": [{"type", "text"}, ...]}, ...]}
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT,
            directory TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
    """)
    for s in sessions:
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?)",
            (s["id"], s.get("parent_id"), s.get("title", ""),
             s.get("directory", ""), s["created"], s["updated"]),
        )
        for i, m in enumerate(s["messages"]):
            conn.execute(
                "INSERT INTO message VALUES (?,?,?,?)",
                (m["id"], s["id"], s["created"] + i,
                 json.dumps({"role": m["role"]})),
            )
            for j, p in enumerate(m["parts"]):
                conn.execute(
                    "INSERT INTO part (id, message_id, session_id, time_created, data)"
                    " VALUES (?,?,?,?,?)",
                    (p.get("id", f"{m['id']}_p{j}"), m["id"], s["id"],
                     s["created"] + i, p.get("data", json.dumps(
                         {"type": p["type"], "text": p.get("text", "")}))),
                )
    conn.commit()
    return conn


SAMPLE_SESSION = {
    "id": "ses_abc123",
    "title": "fixing the widget",
    "directory": "/work/proj",
    "created": 1_700_000_000_000,
    "updated": 1_700_000_500_000,
    "messages": [
        {"id": "msg_1", "role": "user", "parts": [
            {"type": "text", "text": "hello needle"}]},
        {"id": "msg_2", "role": "assistant", "parts": [
            {"type": "reasoning", "text": "thinking..."},
            {"type": "text", "text": "working on it"},
            {"type": "tool", "text": "tool output"},
            {"type": "step-start", "text": ""}]},
    ],
}


class TestParseOpencodeSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])

    def test_extracts_only_user_and_assistant_text_parts(self):
        (metadata, messages), = list(recall.parse_opencode_sessions(self.db))
        self.assertEqual([role for role, _ in messages], ["user", "assistant"])
        self.assertIn("needle", messages[0][1])
        self.assertIn("working on it", messages[1][1])
        self.assertNotIn("thinking", messages[1][1])
        self.assertNotIn("tool output", messages[1][1])

    def test_metadata_shape(self):
        (metadata, _), = list(recall.parse_opencode_sessions(self.db))
        self.assertEqual(metadata["session_id"], "ses_abc123")
        self.assertEqual(metadata["source"], "opencode")
        self.assertEqual(metadata["project"], "/work/proj")
        self.assertEqual(metadata["slug"], "fixing the widget")
        self.assertEqual(metadata["timestamp"], 1_700_000_000_000)
        self.assertEqual(metadata["file_path"], f"{self.db}#ses_abc123")
        self.assertEqual(metadata["mtime"], 1_700_000_500.0)

    def test_slug_falls_back_to_id_when_title_empty(self):
        s = dict(SAMPLE_SESSION, id="ses_noitle", title="")
        db = str(Path(self.tmp.name) / "empty_title.db")
        make_opencode_db(db, [s])
        (metadata, _), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(metadata["slug"], "ses_noitle")

    def test_session_with_no_text_parts_is_still_yielded(self):
        s = {"id": "ses_empty", "title": "t", "directory": "/w",
             "created": 1, "updated": 1,
             "messages": [{"id": "m", "role": "assistant", "parts": [
                 {"type": "step-start", "text": ""}]}]}
        db = str(Path(self.tmp.name) / "empty.db")
        make_opencode_db(db, [s])
        (metadata, messages), = list(recall.parse_opencode_sessions(db))
        self.assertEqual(messages, [])

    def test_malformed_part_json_is_skipped(self):
        s = dict(SAMPLE_SESSION, id="ses_bad")
        s["messages"] = [
            {"id": "m1", "role": "user", "parts": [
                {"type": "text", "text": "ok", "data": "{not json"},
                {"type": "text", "text": "kept"}]},
        ]
        db = str(Path(self.tmp.name) / "bad.db")
        make_opencode_db(db, [s])
        _, messages = list(recall.parse_opencode_sessions(db))[0]
        self.assertEqual(messages, [("user", "kept")])

    def test_reads_live_wal_database_while_a_writer_holds_it(self):
        db = str(Path(self.tmp.name) / "wal.db")
        writer = make_opencode_db(db, [SAMPLE_SESSION])
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO session VALUES ('ses_live',NULL,'t','/w',1,2)"
        )
        writer.commit()  # keep `writer` open — the -wal file stays on disk
        self.addCleanup(writer.close)
        ids = [md["session_id"] for md, _ in recall.parse_opencode_sessions(db)]
        self.assertIn("ses_live", ids)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_opencode_parser.TestParseOpencodeSessions -v`
Expected: FAIL with `AttributeError: module 'recall' has no attribute 'parse_opencode_sessions'`.

- [ ] **Step 3: Implement the parser in `scripts/recall.py`**

Add below the resolver (above `index_sessions`). `_open_opencode_db` must try a read-only URI first and fall back to a plain connection (WAL/shm edge cases):

```python
def _open_opencode_db(db_path):
    """Open the opencode DB read-only, falling back to a plain read connection.

    mode=ro over a URI avoids ever touching the file; some WAL setups refuse it,
    in which case a normal connection is used and only SELECTs are issued.
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.OperationalError:
        return sqlite3.connect(str(db_path))


def parse_opencode_sessions(db_path):
    """Yield (metadata, messages) for every session in an opencode database.

    Reads the ``session``/``message``/``part`` tables and keeps user/assistant
    text parts only. The database is opened read-only and finally closed.
    """
    conn = _open_opencode_db(db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, directory, time_created, time_updated FROM session"
        ).fetchall()
        for session_id, title, directory, created, updated in rows:
            messages = []
            for (mdata, pdata) in conn.execute(
                "SELECT m.data, p.data FROM message m JOIN part p "
                "ON p.message_id = m.id WHERE m.session_id = ? "
                "ORDER BY m.time_created, p.time_created",
                (session_id,),
            ):
                try:
                    role = json.loads(mdata).get("role")
                    part = json.loads(pdata)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    continue
                if role not in ("user", "assistant"):
                    continue
                if part.get("type") != "text":
                    continue
                text = part.get("text", "")
                if text:
                    messages.append((role, text))

            slug = title or (session_id[:12] if session_id else "")
            metadata = {
                "session_id": session_id,
                "source": "opencode",
                "file_path": f"{db_path}#{session_id}",
                "project": directory or "",
                "slug": slug,
                "timestamp": created or 0,
                "mtime": (updated or 0) / 1000.0,
            }
            yield metadata, messages
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_opencode_parser -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/recall.py tests/test_opencode_parser.py
git commit -m "feat(opencode): parse sessions from the opencode database"
```

---

### Task 3: Index opencode sessions and expose `--source opencode`

**Files:**
- Modify: `scripts/recall.py` — `index_sessions()` (add opencode step + counts), new `index_opencode()`, argparse `--source` choices and description strings, module docstring/description.
- Modify: `tests/test_ranking.py:35-41`, `tests/test_role_column.py:45-51` (add `OPENCODE_DB` to the patched dirs).
- Test: `tests/test_opencode_parser.py` (append)

**Interfaces:**
- Consumes: `parse_opencode_sessions()` (Task 2), `OPENCODE_DB` (Task 1).
- Produces: `index_opencode(conn, force=False) -> tuple[int, int]` returning `(indexed, skipped)`; `index_sessions()` return counts now include opencode.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opencode_parser.py`:

```python
class TestIndexingOpencode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])

        for name, value in (("CLAUDE_PROJECTS_DIR", self.root / "none"),
                            ("CODEX_SESSIONS_DIR", self.root / "none"),
                            ("PI_SESSIONS_DIR", self.root / "none"),
                            ("GROK_SESSIONS_DIR", self.root / "none"),
                            ("CURSOR_PROJECTS_DIR", self.root / "none"),
                            ("OPENCODE_DB", Path(self.db))):
            self.addCleanup(setattr, recall, name, getattr(recall, name))
            setattr(recall, name, value)

        self.index = sqlite3.connect(str(self.root / "index.db"))
        recall.create_schema(self.index)
        recall.migrate_schema(self.index)

    def test_indexes_and_searches_opencode(self):
        recall.index_sessions(self.index)
        results = recall.search(self.index, "needle")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][1], "opencode")
        self.assertEqual(results[0][0], "ses_abc123")

    def test_source_filter(self):
        recall.index_sessions(self.index)
        rows = recall.list_sessions(self.index, source="opencode")
        self.assertEqual([r[0] for r in rows], ["ses_abc123"])

    def test_unchanged_session_is_skipped_on_second_run(self):
        recall.index_sessions(self.index)
        _, skipped, *_ = recall.index_sessions(self.index)
        self.assertGreaterEqual(skipped, 1)

    def test_force_reindex_rebuilds(self):
        recall.index_sessions(self.index)
        indexed, _, *_ = recall.index_sessions(self.index, force=True)
        self.assertEqual(indexed, 1)

    def test_missing_db_is_skipped_without_error(self):
        recall.OPENCODE_DB = self.root / "does-not-exist.db"
        indexed, skipped, *_ = recall.index_sessions(self.index)
        self.assertEqual(indexed, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_opencode_parser.TestIndexingOpencode -v`
Expected: FAIL — opencode sessions are not indexed (`test_indexes_and_searches_opencode` finds 0 results).

- [ ] **Step 3: Implement `index_opencode()` and wire it in**

Add above `index_sessions()`:

```python
def index_opencode(conn, force=False):
    """Index opencode sessions from OPENCODE_DB. Returns (indexed, skipped)."""
    db = OPENCODE_DB
    if not db or not Path(db).is_file():
        return 0, 0

    try:
        parsed = list(parse_opencode_sessions(db))
    except sqlite3.Error as e:
        print(f"Warning: skipping opencode database {db}: {e}", file=sys.stderr)
        return 0, 0

    existing = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT session_id, mtime FROM sessions WHERE source = 'opencode'"
        )
    }

    indexed = 0
    skipped = 0
    for metadata, messages in parsed:
        sid = metadata["session_id"]
        if not force and existing.get(sid) == metadata["mtime"]:
            skipped += 1
            continue

        for table in ("sessions", "messages", "messages_cjk"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))

        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, source, file_path, project, slug, timestamp, mtime) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, metadata["source"], metadata["file_path"], metadata["project"],
             metadata["slug"], metadata["timestamp"], metadata["mtime"]),
        )
        msg_rows = [(sid, role, text) for role, text in messages]
        conn.executemany(
            "INSERT INTO messages (session_id, role, text) VALUES (?, ?, ?)", msg_rows
        )
        cjk_rows = [r for r in msg_rows if has_cjk(r[2])]
        if cjk_rows:
            conn.executemany(
                "INSERT INTO messages_cjk (session_id, role, text) VALUES (?, ?, ?)",
                cjk_rows,
            )
        indexed += 1

    return indexed, skipped
```

In `index_sessions()`, call it after the file loop and fold counts in. Change the block right before the first `conn.commit()` (currently around line 861) to:

```python
    opencode_indexed, opencode_skipped = index_opencode(conn, force=force)
    indexed += opencode_indexed
    skipped += opencode_skipped

    conn.commit()
```

Update argparse `--source` (currently lines 1078–1082) and the two description strings:

```python
    parser = argparse.ArgumentParser(
        description="Search past Claude Code, Codex, pi, Grok, Cursor Agent, and opencode sessions"
    )
    ...
    parser.add_argument(
        "--source",
        choices=["claude", "codex", "pi", "grok", "cursor", "opencode"],
        help="Filter by source (claude, codex, pi, grok, cursor, or opencode)",
    )
```

Update the module docstring (line 2) to include opencode.

- [ ] **Step 4: Guard the existing fixtures**

In `tests/test_ranking.py` (the loop at lines 35–41) and `tests/test_role_column.py` (lines 45–51), add `("OPENCODE_DB", self.root / "none")` to the tuple list so those tests never index the host's real opencode DB.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest discover tests -v`
Expected: PASS, including the new `TestIndexingOpencode` and the untouched ranking/role tests.

- [ ] **Step 6: Commit**

```bash
git add scripts/recall.py tests/test_opencode_parser.py tests/test_ranking.py tests/test_role_column.py
git commit -m "feat(opencode): index opencode sessions and add --source opencode"
```

---

### Task 4: Read an opencode session from its `db#id` reference

**Files:**
- Modify: `scripts/read_session.py` — `detect_format()` and `iter_messages()`.
- Test: `tests/test_opencode_parser.py` (append)

**Interfaces:**
- Consumes: `make_opencode_db`, `SAMPLE_SESSION` (test helpers), the `db#id` `file_path` format from Task 2.
- Produces: `read_session.detect_format(path) -> "opencode"`; `iter_messages(path)` yields `(role, text)` for opencode references.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opencode_parser.py`:

```python
class TestReadSessionOpencode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "opencode.db")
        make_opencode_db(self.db, [SAMPLE_SESSION])
        self.ref = f"{self.db}#ses_abc123"

    def test_detects_opencode_reference(self):
        self.assertEqual(read_session.detect_format(self.ref), "opencode")

    def test_reads_user_and_assistant_text(self):
        msgs = list(read_session.iter_messages(self.ref))
        self.assertEqual([role for role, _ in msgs], ["user", "assistant"])
        self.assertIn("needle", msgs[0][1])

    def test_db_path_with_spaces_and_hash_in_name(self):
        spaced = self.tmp.name + "/Application Support/opencode.db"
        Path(spaced).parent.mkdir(parents=True, exist_ok=True)
        make_opencode_db(spaced, [SAMPLE_SESSION])
        ref = f"{spaced}#ses_abc123"
        self.assertEqual(read_session.detect_format(ref), "opencode")
        self.assertEqual(len(list(read_session.iter_messages(ref))), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_opencode_parser.TestReadSessionOpencode -v`
Expected: FAIL — `detect_format` tries to open the `db#id` string as a file and raises `FileNotFoundError`.

- [ ] **Step 3: Implement the reader branch**

In `scripts/read_session.py`, add a helper and an early return in `detect_format` (before the existing cursor/grok checks):

```python
def _opencode_ref(path):
    """Return (db_path, session_id) for a 'db#id' opencode reference, else None."""
    text = str(path)
    db, sep, session_id = text.rpartition("#")
    if sep and db.endswith(".db"):
        return db, session_id
    return None
```

At the top of `detect_format`:

```python
    if _opencode_ref(path):
        return "opencode"
```

At the top of `iter_messages`, before `fmt = detect_format(path)`:

```python
    ref = _opencode_ref(path)
    if ref:
        from recall import parse_opencode_sessions

        db_path, session_id = ref
        for metadata, messages in parse_opencode_sessions(db_path):
            if metadata["session_id"] == session_id:
                yield from messages
                return
        return
```

(The local import mirrors how `read_session` already sits beside `recall` in `scripts/`; it avoids importing `recall` for the common file-based path.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_opencode_parser -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/read_session.py tests/test_opencode_parser.py
git commit -m "feat(opencode): read opencode sessions by db#id reference"
```

---

### Task 5: Documentation

**Files:**
- Modify: `SKILL.md`, `README.md`, `CHANGELOG.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `SKILL.md`**

- Frontmatter `description`: change to `Search past Claude Code, Codex, pi, Grok, Cursor Agent, and opencode sessions.`
- Title and intro: add opencode to the source list.
- Usage line: add `opencode` to the `--source` list.
- Add an example:
  ```bash
  # Search only opencode sessions
  python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source opencode
  ```
- Add to "After Finding a Match":
  ```bash
  # opencode sessions [opencode]
  cd /path/to/project
  opencode --session SESSION_ID
  ```
- Notes: add `- `~/.local/share/opencode/opencode.db` — opencode`; update "Indexes N sources"; add `or `[opencode]`` to the tag sentence; add `- **Upgrading from 0.4.x**: run `--reindex` once to pull in opencode sessions`.
- Add a new "opencode-specific behavior" section:
  ```markdown
  ## opencode-specific behavior

  - Sessions live in a single SQLite database (`opencode.db`), not per-session files; `read_session.py` reads it from a `<db>#<session-id>` reference.
  - Data directory is resolved per OS: `$XDG_DATA_HOME/opencode` (Linux), `~/Library/Application Support/opencode` (macOS), `%LOCALAPPDATA%\opencode\Data` (Windows); override with `OPENCODE_DB`.
  - The database is opened read-only. Subagent sessions are indexed as their own sessions.
  ```

- [ ] **Step 2: Update `README.md`**

- Intro sentence and source list: add opencode.
- Index diagram: add a line for the opencode DB (label it as a SQLite source, not a `*.jsonl` glob).
- Bullets: add `opencode` to the results-tag line.

- [ ] **Step 3: Update `CHANGELOG.md`**

Under `## Unreleased`, append:

```markdown
- Add **opencode** session support — indexes sessions from opencode's SQLite
  database (`opencode.db`), resolved per OS or via `OPENCODE_DB`
- New `--source opencode` filter; results tagged `[opencode]`
- Sessions are read from `session`/`message`/`part` tables, keeping user/assistant
  text parts only; subagent sessions are indexed as their own sessions
- `read_session.py` reads opencode sessions from a `<db>#<session-id>` reference
- Resume: `opencode --session <id>` (see SKILL.md)
- Run `--reindex` once to pull opencode sessions into the index
```

- [ ] **Step 4: Run the full suite and a manual smoke test**

Run: `python3 -m unittest discover tests -v`
Expected: PASS.

Run (on a host with opencode history):
`python3 scripts/recall.py --days 30 --source opencode --limit 5`
Expected: lists opencode sessions; `python3 scripts/read_session.py "$(python3 scripts/recall.py 'test' --source opencode --limit 1 | ...)"` prints turns (manual check).

- [ ] **Step 5: Commit**

```bash
git add SKILL.md README.md CHANGELOG.md
git commit -m "docs(opencode): document opencode session support"
```

---

## Self-Review Notes

- **Spec coverage:** resolver (§1 → Task 1), parser (§2 → Task 2), indexer + `--source` (§3 → Task 3), reader + resume (§4 → Task 4), docs (§5 → Task 5), tests incl. the ranking/role guard (§6 → Tasks 2/3).
- **Type consistency:** `parse_opencode_sessions`, `index_opencode`, `opencode_data_dir`, `resolve_opencode_db`, `OPENCODE_DB` names match across tasks; metadata keys match the existing parsers plus `mtime`.
- **Review Focus:** each of the five inputs has a named test in Tasks 1–4 (marked above).
