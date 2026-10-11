# opencode session support for recall — design

Date: 2026-10-10
Status: approved for planning
Branch: `opencode-support` (based on `feat/cursor-agent-support`)

## Intent

recall indexes past Claude Code, Codex, pi, Grok and Cursor Agent sessions so an
agent can full-text search them. Add **opencode** as a sixth source, built on top
of the Cursor branch so both land together.

opencode differs from every existing source: it does not write one JSONL file per
session. It keeps every session, message and content part in a **single SQLite
database** (`opencode.db`). That is the whole reason this is not a copy-paste of
the Cursor parser — it needs a database reader and, because there is no transcript
file, a database-aware `read_session.py`.

Deliverable: a new branch, `opencode-support`, on `driftfox/recall`. No PR yet.

## Constraints

- No new dependencies: Python 3.9+ stdlib only (`sqlite3`, `json`, `pathlib`),
  matching the rest of recall.
- Must work on every OS opencode supports (Linux, macOS, Windows). recall already
  runs on Windows, so no POSIX-only assumptions.
- Must never write to or lock opencode's database.
- Must stay a clean, small diff on top of the Cursor branch: the file-scan
  pipeline stays untouched; opencode is isolated in its own function.
- Follow the Cursor parser's conventions (skip thinking/tools, metadata shape,
  `--source`, docs, tests).

## Design

### 1. Resolving the database path (cross-platform)

opencode picks its data directory per-OS (the standard `env-paths` convention,
confirmed by inspecting the `opencode` binary):

| OS      | data directory                                            |
|---------|-----------------------------------------------------------|
| Linux   | `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode`    |
| macOS   | `~/Library/Application Support/opencode`                  |
| Windows | `%LOCALAPPDATA%\opencode\Data`                            |

A module-level `resolve_opencode_db()` returns the default DB, in this order:

1. `OPENCODE_DB` env var, if set — `:memory:` means "no DB"; an absolute path is
   used as-is; a bare filename is resolved inside the data directory.
2. `<data dir>/opencode.db` if it exists (stable channel).
3. Otherwise the newest `<data dir>/opencode-*.db` (non-stable channels name the
   file `opencode-<channel>.db`).

The result is stored in a module-level `OPENCODE_DB` so tests can point it at a
synthetic database, mirroring how `CURSOR_PROJECTS_DIR` is overridden in tests.

### 2. Reading sessions (`parse_opencode_sessions`)

Open the DB **read-only** with a URI connection
(`sqlite3.connect(f"file:{Path(db).as_uri()... }?mode=ro", uri=True)`, encoded
with `Path.as_uri()` so spaces like `Application Support` survive). If the
read-only open is refused (WAL shared-memory edge cases), fall back to a plain
read connection; we only ever `SELECT`.

For every row in `session` (`id`, `parent_id`, `title`, `directory`,
`time_created`, `time_updated`), walk its messages and text parts:

```sql
SELECT m.data, p.data
FROM message m JOIN part p ON p.message_id = m.id
WHERE m.session_id = ?
ORDER BY m.time_created, p.time_created
```

Parse both `data` blobs as JSON **in Python** (recall parses JSON in Python
everywhere; no SQL JSON1 dependency). Keep only:

- messages whose `role` is `user` or `assistant`, and
- parts whose `type` is `text`.

Skip `reasoning`, `tool`, `step-start`, `step-finish`, `patch` and everything
else — the same "skip thinking and tools" rule the other parsers follow.

Metadata per session:

| field       | value                                             |
|-------------|---------------------------------------------------|
| `session_id`| `session.id`                                      |
| `source`    | `"opencode"`                                      |
| `file_path` | `f"{db_path}#{session.id}"` (see reader, below)   |
| `project`   | `session.directory`                               |
| `slug`      | `session.title`, else the first 12 chars of id    |
| `timestamp` | `session.time_created`                            |
| `mtime`     | `session.time_updated / 1000` (dedup key)         |

Child/subagent sessions are indexed as their own searchable sessions (they are
distinct `session` rows with a `parent_id`); nothing is folded into a parent.

### 3. Folding into the indexer (`index_opencode`)

`index_sessions()` keeps its file-scan loop unchanged. Immediately after the loop
and before the existing FTS5 `optimize`/commit block, it calls
`index_opencode(conn, force)` and folds the counts in.

`index_opencode`:

- returns `(0, 0)` with no error if `OPENCODE_DB` is missing or not a file;
- reads existing dedup state: `SELECT session_id, mtime FROM sessions WHERE
  source = 'opencode'`;
- for each parsed session, skips when the stored `mtime` equals the new one and
  `force` is false; otherwise deletes any existing rows for that `session_id`
  (sessions, messages, messages_cjk) and inserts fresh — reusing the same
  insert + CJK-splitting logic as the file loop;
- commits once.

Because it runs before the shared `optimize`, its messages are merged into the
FTS5 index exactly like every other source. `--reindex` (force) wipes all tables
first, so opencode rebuilds with everything else.

`--source` gains `opencode` (argparse `choices` + help text).

### 4. Reader and resume

There is no transcript file, so the stored `file_path` is
`"<db path>#<session id>"`. `read_session.py`:

- `detect_format()` returns `"opencode"` for any path containing a `#` whose left
  side ends in `.db` (checked before the file-open fallback);
- `iter_messages()` gains an opencode branch that splits on the last `#`, opens
  the DB read-only, and yields the same `(role, text)` pairs as the parser —
  so `--pretty` and JSON output work unchanged.

Resume hint added to `SKILL.md`:

```bash
# opencode sessions [opencode]
cd /path/to/project
opencode --session SESSION_ID
```

### 5. Docs

- `SKILL.md`: title/description, usage line, an `--source opencode` example,
  resume block, Notes list (six sources + upgrade note).
- `README.md`: sources diagram/bullets, `[opencode]` tag mention.
- `CHANGELOG.md`: entry under `Unreleased` (version left for the maintainer).

### 6. Tests (`tests/test_opencode_parser.py`)

A throwaway `opencode.db` is built inside `tmpdir` with `session`, `message` and
`part` tables and JSON `data` blobs (no committed fixtures), then
`recall.OPENCODE_DB` is pointed at it. Covered:

- extraction keeps user/assistant text and skips `reasoning`/`tool`/`step` parts;
- metadata (id, project from `directory`, slug from `title`, timestamp);
- search and list mode return the session under `--source opencode`;
- `read_session.detect_format` and `iter_messages` handle the `db#id` reference;
- a missing/None DB is skipped without raising;
- an optional integration test against a real `~/.local/share/opencode/opencode.db`
  that skips when absent.

The `test_ranking.py` and `test_role_column.py` fixtures also point
`recall.OPENCODE_DB` at an empty path (as the Cursor branch did for
`CURSOR_PROJECTS_DIR`) so they never index the real database.

## Out of scope

- The registry/plugin refactor (option B) to support "many CLI agents" — a
  separate future PR.
- Shelling out to `opencode export`, or mirroring sessions to JSONL files.
- Indexing tool output, reasoning traces, or non-conversational parts.
- Multi-DB / multi-user discovery beyond the single resolved database.

## Risks / notes

- Non-stable opencode channels use `opencode-<channel>.db`; the fallback glob
  covers them.
- Reading a live WAL database read-only can occasionally be refused on Windows;
  the plain-connection fallback handles it, and `SELECT`-only access is safe.
- `session.time_updated` is the dedup key, so an in-progress session is
  re-indexed on its next change and left alone once idle.
