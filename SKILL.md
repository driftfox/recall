---
name: recall
description: >
  Search past Claude Code, Codex, pi, Grok, opencode and omp sessions. Triggers: /recall, "search old conversations",
  "find a past session", "recall a previous conversation", "search session history",
  "what did we discuss", "remember when we"
metadata:
  author: arjunkmrm
  version: "0.7.0"
  license: MIT
---

# /recall — Search Past Claude, Codex, pi, Grok, opencode & omp Sessions

Search all past Claude Code, Codex, pi, Grok, opencode and omp sessions using full-text search with BM25 ranking.

## Usage

```bash
python3 ~/.claude/skills/recall/scripts/recall.py [QUERY] [--project PATH] [--days N] [--source claude|codex|pi|grok|opencode|omp] [--limit N] [--reindex]
```

## Examples

```bash
# List every session in the last day (no text search)
python3 ~/.claude/skills/recall/scripts/recall.py --days 1

# List every pi session in the last week
python3 ~/.claude/skills/recall/scripts/recall.py --days 7 --source pi

# Simple keyword search
python3 ~/.claude/skills/recall/scripts/recall.py "bufferStore"

# Phrase search (exact match)
python3 ~/.claude/skills/recall/scripts/recall.py '"ACP protocol"'

# Boolean query
python3 ~/.claude/skills/recall/scripts/recall.py "rust AND async"

# Prefix search
python3 ~/.claude/skills/recall/scripts/recall.py "buffer*"

# Filter by project and recency
python3 ~/.claude/skills/recall/scripts/recall.py "state machine" --project ~/my-project --days 7

# Search only Claude Code sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source claude

# Search only Codex sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source codex

# Search only pi sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source pi

# Search only Grok sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source grok

# Search only opencode sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source opencode

# Search only omp sessions
python3 ~/.claude/skills/recall/scripts/recall.py "buffer" --source omp

# Force reindex
python3 ~/.claude/skills/recall/scripts/recall.py --reindex "test"
```

## Query Syntax (FTS5)

- **Words**: `bufferStore` — matches stemmed variants (e.g., "discussing" matches "discuss")
- **Phrases**: `"ACP protocol"` — exact phrase match
- **Boolean**: `rust AND async`, `tauri OR electron`, `NOT deprecated`
- **Prefix**: `buffer*` — matches bufferStore, bufferMap, etc.
- **Combined**: `"state machine" AND test`
- **CJK**: `issue化` — automatically uses trigram matching for Japanese/Chinese/Korean text

## After Finding a Match

To resume a session, `cd` into the project directory and use the appropriate command:

```bash
# Claude Code sessions [claude]
cd /path/to/project
claude --resume SESSION_ID

# Codex sessions [codex]
cd /path/to/project
codex resume SESSION_ID

# Grok sessions [grok]
cd /path/to/project
grok --resume SESSION_ID

# Pi sessions [pi]
cd /path/to/project
pi --session SESSION_ID         # full or partial id; pi resolves prefix matches

# opencode sessions [opencode]
cd /path/to/project
opencode --session SESSION_ID   # the ses_... id; opencode keeps sessions in one SQLite database

# omp sessions [omp]
cd /path/to/project
omp --resume SESSION_ID         # the session uuid; omp resolves prefix matches
```

Each result includes a `File:` path. Use it to read the raw transcript (auto-detects format):

```bash
python3 ~/.claude/skills/recall/scripts/read_session.py <File-path-from-result>
```

For opencode the `File:` path is `<database>#<session-id>`; `read_session.py` also
accepts a bare `ses_...` id and reads opencode's default database.

If results are missing `File:` paths, run `--reindex` to backfill.

## Notes

- Index is stored at `~/.recall.db` (SQLite FTS5, auto-migrated from `~/.claude/recall.db`)
- Indexes six sources: `~/.claude/projects/` (Claude Code), `~/.codex/sessions/` (Codex), `~/.pi/agent/sessions/` (pi), `~/.grok/sessions/**/chat_history.jsonl` (Grok), `~/.local/share/opencode/opencode.db` (opencode), and `~/.omp/agent/sessions/` (omp)
- First run indexes all sessions (a few seconds); subsequent runs are incremental
- Only user and assistant messages are indexed (tool calls, thinking blocks, state snapshots skipped)
- Results show `[claude]`, `[codex]`, `[pi]`, `[grok]`, `[opencode]`, or `[omp]` tags to indicate the source
- Dual-table FTS: English queries use Porter stemming, CJK queries use trigram matching
- Omit the query argument for **list mode** — every session in the window, sorted by recency, no FTS
- Provide a query for full-text search; both modes accept `--project`, `--days`, `--source`, `--limit`
- opencode keeps every session in one SQLite database rather than a file apiece; `OPENCODE_DB` points recall at a specific database and `OPENCODE_DATA_DIR` at its data directory
- omp keeps one JSONL file per session under `~/.omp/agent/sessions/`; subagent transcripts are indexed too, and `OMP_SESSION_DIR` points recall at a different sessions directory
- **Upgrading from 0.3.x**: run `--reindex` once to pull in pi sessions
- **Upgrading from 0.4.x**: run `--reindex` once to pull in Grok sessions
- **Upgrading from 0.5.x**: run `--reindex` once to pull in opencode sessions
- **Upgrading from 0.6.x**: run `--reindex` once to pull in omp sessions
- **Upgrading from 0.2.x**: run `--reindex` once to build the CJK index
