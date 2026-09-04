# delegation-core — Agent Guide

This document is written for AI assistants connecting to the `delegation-core` MCP server.
Read it fully at the start of any session where these tools are available.

---

## What this is

`delegation-core` is a local AI workhorse running on the user's machine.
It has a vector search index of the user's markdown vault, a local language model (llama.cpp),
and persistent storage. The vault is plain markdown on disk — Obsidian is no longer
the intended reader, and the Tauri dashboard is the primary surface. Do not write
for Obsidian's constraints; `[[wikilinks]]` stay because the vault graph is built
from them, not because Obsidian renders them.

It exists so that **you do not have to do the heavy lifting**.

Your role is orchestration. delegation-core's role is execution.

Every token you spend summarizing, classifying, compressing, or recalling information
that should be in the vault is wasted. delegation-core handles all of that locally at
zero marginal cost. Push work to it aggressively.

---

## Tools

### `capabilities()`
**Call this first, before `heartbeat()`, on every session.**

Returns what this server can actually do, generated rather than written: the live
tool list (asked of the running server), every graph exporter and which tool
reaches it, the ones deliberately unexposed and why, capabilities that exist in
the code but are still unwired, and the search scopes.

```json
→ { "tool_count": 54,
    "tools": [{ "name": "search_vault", "summary": "..." }, ...],
    "graph_exports": { "wired": {...}, "not_exposed": {...} },
    "known_unwired": {...},
    "search_scopes": {...} }
```

### Tools this document does not describe

`capabilities()` returned 54 tools on 2026-09-03; the prose below covers roughly
two thirds of them. The families missing entirely, so they are at least findable:

| Family | Tools | What it is for |
|---|---|---|
| Local queue | `local_task_submit` / `_status` / `_list` / `_cancel` | Hand work to the local model and get a task id back instead of blocking |
| Windows | `window_open` / `window_close` / `window_list` | Mount and unmount MCP servers to free the context they occupy |
| Workspaces | `workspace_save` / `workspace_apply` / `workspace_list` | Named sets of mounted servers |
| Graph | `graph_preview`, `graph_build(_bg)`, `graph_export`, `graph_report`, `graph_affected`, `graph_list`, `graph_hook_install` / `_status` / `_uninstall`, `extract_source_maps` | Code knowledge graphs |
| Vault | `vault_find_notes` (literal match, no embeddings), `vault_note_links`, `vault_rename_note`, `vault_health_detail` | |
| Ingest | `ingest_forget` | The exact inverse of `ingest_folder` — this is what makes an ingest reversible |

**Sidecar routing, also undocumented.** `sidecar.py` reads a `<stem>.meta.yaml`
dropped beside a file in `_inbox/`. Keys `folder_hint` (bypasses the classifier
entirely) and `no_merge` (never merge this into an existing note). It is the
manual routing control the inbox pipeline otherwise lacks.

**This document is not authoritative; that report is.** Prose drifts from code
with nothing to catch it — every numeric constant in this guide is a copy that
no test compares against its source. `capabilities()` is generated at call time
and guarded by `tests/test_capability_registry.py`, which fails when a new
artifact-producing function appears unclassified. When the two disagree, the
tool is right and this file is stale.

---

### `heartbeat()`
Check that llama.cpp and ChromaDB are online.  
Call this after `capabilities()`. If status is `degraded`, warn the user before proceeding.

```json
→ { "status": "healthy", "llama_cpp": "online", "vault": { "indexed_notes": 247, ... } }
```

---

### `search_vault(query, limit=5)`
Semantic search across all vault notes using BGE embeddings.  
Results are pre-compressed by llama.cpp before being returned — you receive a tight summary,
not raw documents. Token cost is flat regardless of vault size.

```json
→ {
    "query": "Q3 infrastructure decisions",
    "summary": "Three decisions logged: ...",   ← already compressed by llama.cpp
    "sources": [{ "title": "...", "path": "...", "similarity": 0.87, "snippet": "..." }]
  }
```

**Use `summary` for reasoning. Use `sources` only if you need to cite a specific note.**

---

### `read_note(note_name)`
Read a specific note by filename stem. Accepts partial, case-insensitive matches.  
Returns raw markdown. Use when you need the full content of one known note.

```json
→ "---\ntitle: Q3 Infrastructure Decision\ndate: 2026-05-14\n---\n\n..."
```

---

### `write_note(folder, title, content)`
Write a markdown note to the vault and index it immediately with BGE embeddings.
The note is searchable in the same session as soon as it is written.

Valid folders are the `heartbeat()` → `vault.folder_counts` keys, and are matched
**case-insensitively** — passing `write_note(folder="decisions")` or `write_note(folder="Decisions")`
automatically resolves to the vault's configured folder casing. On this vault they are exactly
these six: `decisions`, `research`, `tools`, `fixes`, `reference`, `sessions`.

`research` **does** exist here — an earlier version of this file said it did not and routed
research notes to `reference`; that was wrong for this install. `Projects`, `Procedures`,
`Scratch` and `Infrastructure` do **not** exist and a write to any of them fails. Corrected
2026-09-03 against `heartbeat().vault.folder_counts`, which is the authority.

```json
→ { "status": "ok", "path": "2026-06-03-Q3-budget-decision.md", "folder": "Decisions" }
```

**Write notes liberally.** Every significant decision, insight, meeting outcome, or research
finding should be persisted. The vault is the user's permanent memory.

---

### `compress(source, raw_content)`
Send raw text to llama.cpp for compression. Returns only key facts, decisions, and action items.
Maximum input: 6000 characters per call. For longer content, chunk it.

```json
→ { "source": "email thread", "compressed": "Decision: migrate to new vendor by Q4. ..." }
```

**Use before processing any large input.** Do not read a long document and summarize it
yourself — compress it first, then reason over the result.

---

### `vault_stats()`
Returns note counts per folder and ChromaDB index size. Use to orient the user or
confirm that a write was persisted.

```json
→ { "indexed_notes": 247, "folder_counts": { "Decisions": 34, "Reference": 89, ... } }
```

---

### `list_mcp_clients()`
List which MCP client surfaces are currently connected to this delegation-core process —
Claude Code, Claude Desktop, Codex, Antigravity, or anything else that speaks MCP and has
delegation-core configured. Each running `delegation-core run` instance writes its own
heartbeat file; this reads all of them and drops any that have gone stale (no activity in
the last 2 minutes). Note this only covers connections **to delegation-core itself** — it
has no visibility into other MCP servers (e.g. ClickUp, Gmail) a client also has configured.
For that, the client's own MCP list (e.g. `claude mcp list`) is authoritative.

```json
→ { "clients": [
      { "pid": 48213, "client_name": "claude-code", "client_version": "1.4.2",
        "first_seen": "2026-07-27T14:02:11+00:00", "last_seen": "2026-07-27T14:19:03+00:00",
        "tool_calls": 12, "seconds_since_active": 4 }
  ]}
```

Use when the user asks "what's connected right now" or "is Desktop still open" — this reads
live process state, not the vault, so `search_vault` won't help here.

---

### `export_session(title, summary, key_decisions="")`
Write a curated session summary to the vault's `Sessions/` folder and index it immediately.  
**Call this when the user signals the session is ending** — any variation of goodbye, thanks,
we're done, wrapping up, see you tomorrow, etc. Do not wait to be asked.

```
title:         short phrase describing what this session accomplished
summary:       2–4 sentences: what was discussed, decided, or built
key_decisions: comma-separated list of decisions, artifacts created, or next steps
```

```json
→ { "status": "ok", "path": "2026-06-03-CRM-evaluation-kickoff.md", "folder": "Sessions" }
```

The note is indexed immediately and will surface in future `search_vault()` calls.
This is the curated digest. The raw transcript backup (if Claude Code hooks are configured)
is saved separately as `{date}-transcript-{short_id}.md`.

**Note:** the raw transcript backup is written by a stdlib-only `SessionEnd` hook that
cannot import the indexing code directly, so it is **not indexed at the moment it's
written**. The hook fires a detached `delegation-core reindex` immediately afterward
(usually done in 1-2s for a vault this size), and the next `SessionStart` hook fires a
second backstop reindex if any notes changed since the last check and the reindex
cooldown has elapsed. In practice the transcript is searchable within a couple of
seconds — but if `search_vault()` runs in that narrow window right after export, a very
recent transcript may not appear yet.

---

### `run_maintenance()`
Process the vault inbox synchronously: classify, merge near-duplicates, add wikilinks,
write weekly summary. Blocks until complete. Use for small inboxes (< ~20 files).
For large inboxes use `run_maintenance_bg()` instead.

```json
→ { "classified": ["notes.md → Reference/"], "merged": [], "errors": [], "junk": [] }
```

`junk` lists boilerplate files (licenses, READMEs, `requirements*.txt`, etc.) that were
moved to `_processed/` without being filed as notes — report these to the user as "skipped,
not notes" rather than as errors.

---

### `search_web(query, num_results=5)`
Search the web via DuckDuckGo and return results compressed by llama.cpp.
Use when the vault has no relevant context and the user needs current or external information.

```json
→ { "query": "FastMCP changelog 2026", "results": "FastMCP 2.3 released May 2026: ..." }
```

**Prefer `search_vault` for recurring topics.** Only fall back to `search_web` if the vault
has nothing relevant or the user explicitly wants live data. After finding useful results,
write them to the vault so future queries are answered locally.

---

## Maintenance tools

### `vault_list_notes(folder, limit=20)`
List notes in a folder sorted newest-first. Returns title, date, path, and file size.
Use to orient yourself before reading or updating specific notes.

```json
→ { "folder": "Decisions", "count": 3, "notes": [
      { "title": "Q3 vendor decision", "date": "2026-06-03", "path": "Decisions/...", "size_bytes": 1240 }
  ]}
```

---

### `vault_inbox_status()`
Show what is waiting in `<vault>/_inbox/` before running maintenance.
Always call this before `run_maintenance` or `run_maintenance_bg` to confirm there is work to do.

```json
→ { "count": 4, "files": [{ "name": "meeting.md", "size_bytes": 980, "modified": "2026-06-03 14:22" }],
    "inbox_path": "/home/.../Claude Vault/_inbox" }
```

---

### `vault_find_similar(note_name, threshold=0.80, limit=5)`
Find notes semantically similar to a given note using BGE embeddings.
Use before merging notes manually, or to discover related content the user may not know exists.

```json
→ { "source_note": "Q3 budget", "threshold": 0.80,
    "similar": [{ "title": "Q3 planning", "similarity": 0.91, "path": "..." }] }
```

---

### `vault_update_note(note_name, append_content)`
Append content to an existing note with a timestamp separator, then re-index immediately.
Use to add follow-up decisions, corrections, or new findings to an existing record rather than
creating a duplicate note.

```json
→ { "status": "ok", "path": "Decisions/2026-06-01-vendor.md", "appended_chars": 312 }
```

---

### `relink_folder(folder, days=None, min_similarity=None, max_links_per_note=8)`
### `relink_folder_bg(folder, days=None, min_similarity=None, max_links_per_note=8)`
Walk any vault subfolder and additively inject `[[wikilinks]]` into existing notes based on
semantic similarity. Use after bulk imports or vault growth to materialise connections that
did not exist at ingestion time. Strictly additive — never removes existing links.
Supports sub-paths (`meetings/Client/2026`).

```json
→ { "folder": "Reference", "relinked": 12, "links_added": 34 }
```

`min_similarity` e o corte de similaridade (nao `threshold`, que nunca
existiu na assinatura). `days` limita a notas tocadas nos ultimos N dias, e
`max_links_per_note` limita quantos `## Related` cada nota recebe.
`tests/test_docs_not_stale.py` falha se esta assinatura voltar a divergir
do que `server.py` expoe.

**Use `relink_folder_bg` on any folder of real size.** The synchronous tool embeds
every note in the folder against every other; on a 31-note folder that ran past the
MCP client's 300-second idle timeout, which aborts the call and drops the connection
while the server keeps working on a job nobody is listening for. The `_bg` variant
returns a `job_id` immediately — poll it with `task_status`.

**Run after a large maintenance pass** to densify the knowledge graph. Also called implicitly
by `run_maintenance` for each processed folder.

---

### `ingest_folder(source_path, recursive=True)`
Index an external directory into ChromaDB **without moving files**.
Use to make an existing folder (project directory, external vault, document archive)
searchable via `search_vault`. The source folder is not modified.

```json
→ { "status": "ok", "indexed": 47, "skipped": 3, "errors": [] }
```

Use `ingest_folder_bg` for large directories to avoid blocking the conversation.

---

### `ingest_status()`
Check the status of the most recent ingest job.

```json
→ { "status": "done", "indexed": 47, "skipped": 3, "errors": [] }
```

---

## Fire-and-forget tools

These tools return a `job_id` immediately and run the work in a background thread.
The server remains fully responsive to other tool calls while the job runs.

**Job IDs are in-memory only.** They are not persisted to disk, so they become
invalid (`task_status` returns `"not found"`) if the MCP server restarts —
e.g. after a config change, a crash, or the user quitting Claude Desktop/Code.
If `task_status` reports a job as not found, treat the underlying work as
unknown rather than failed: re-check with `vault_inbox_status()` /
`vault_stats()`, or simply re-run the `_bg` tool.

### `ingest_folder_bg(source_path, recursive=True)`
Start an external folder ingest in the background. Returns a `job_id` immediately.
Use for large directories where you don't want to block the conversation.

```json
→ { "job_id": "c5a1f3d2", "status": "running",
    "message": "Ingest started. Call task_status(job_id) to check progress." }
```

---

### `run_maintenance_bg()`
Start vault maintenance in the background. Use for large inboxes or when you don't want
to block the conversation while files are being processed.

```json
→ { "job_id": "a3f2b1c4", "status": "running",
    "message": "Maintenance started in background. Call task_status(job_id) to check progress." }
```

---

### `vault_reindex_bg()`
Rebuild the entire ChromaDB index from vault folders in the background.
Use after the user has bulk-added notes to the vault outside of delegation-core,
or after running `delegation-core reindex` from the terminal has been requested.

```json
→ { "job_id": "b7d9e2a1", "status": "running",
    "message": "Reindex started in background. Call task_status(job_id) to check progress." }
```

---

### `task_status(job_id)`
Poll any background job by its ID. Returns current status, elapsed time while running,
and the full result once done.

While a job is running it also reports **how long this kind of job usually takes** and
**when to check again**, both derived from the median of the last 10 successful runs of
that task on this machine:

```json
// While running:
→ { "job_id": "a3f2b1c4", "task": "graph_build", "status": "running",
    "elapsed_seconds": 42, "typical_seconds": 463.0,
    "check_again_in_seconds": 426 }

// When complete:
→ { "job_id": "a3f2b1c4", "task": "run_maintenance", "status": "done",
    "result": { "classified": [...], "merged": [...], "errors": [] },
    "started": "2026-06-03T14:22:01", "finished": "2026-06-03T14:22:38" }
```

**Wait `check_again_in_seconds` instead of polling on a fixed short interval.**
Elapsed time alone cannot tell "20s in, 8 minutes to go" apart from "nearly done", and
a graph build or full reindex on a real corpus runs for minutes — polling it every 30s
spends turns to learn nothing. Both fields are absent the first time a given task runs
on this machine (no history yet); fall back to a conservative wait in that case.

---

## Delegation rules

Apply these without exception when delegation-core is online.

### Always delegate

| Situation | Tool |
|-----------|------|
| User signals the session is ending | `export_session` immediately |
| User asks a question that might have prior context | `search_vault` first, always |
| User shares a long document, email, or paste | `compress` before reading |
| Any decision is made or confirmed | `write_note` to `Decisions/` |
| A meeting happens or is summarized | `write_note` to `Sessions/` |
| Research is completed | `write_note` to `Reference/` |
| A fix, workaround, or solution is found | `write_note` to `Fixes/` |
| A reusable script or prompt is created | `write_note` to `Tools/` |
| User asks "what did we decide about X" | `search_vault` |
| User asks "do we have notes on X" | `search_vault` |
| User wants to add context to an existing note | `vault_update_note` (not a new note) |
| User asks what notes exist on a topic/folder | `vault_list_notes` |
| User asks what's connected to delegation-core right now | `list_mcp_clients` |
| User drops files and wants them organized (small) | `run_maintenance` |
| User drops files and wants them organized (large) | `vault_inbox_status` → `run_maintenance_bg` → poll `task_status` |
| Vault needs rebuilding after bulk import | `vault_reindex_bg` → poll `task_status` |
| Two notes might be duplicates | `vault_find_similar` |
| User needs live/external web information | `search_vault` first, then `search_web` if vault empty |
| A question is about one client and the vault holds several | `search_vault(..., client=...)` |
| User wants to cross-link an existing vault folder (small) | `relink_folder` |
| User wants to cross-link an existing vault folder (large) | `relink_folder_bg` → poll `task_status` |
| User has an external folder to make searchable (small) | `ingest_folder` |
| User has an external folder to make searchable (large) | `ingest_folder_bg` → poll `task_status` |
| `heartbeat().vault_health.needs_repair > 5` | `run_maintenance_bg()` — heal pass runs automatically |

### Never do yourself

- Do not summarize documents you received — use `compress`.
- Do not answer from memory if the vault might have better context — use `search_vault`.
- Do not let a significant output (decision, plan, insight) go unrecorded — use `write_note`.
- Do not build a summary of multiple notes by reading them one by one — use `search_vault`,
  which already compresses the results.
- Do not create a new note when the user is adding to an existing topic — use `vault_update_note`.
- Do not run `run_maintenance` blindly — check `vault_inbox_status` first to confirm there is work.

---

## Session lifecycle

### Startup ritual (run silently before your first response)

```
1. heartbeat()
   → If degraded: notify the user. Do not proceed with vault operations.
   → If healthy: continue.
   → Read processes from heartbeat() — surface any active ones briefly.
   → Check vault_health.needs_repair:
       if > 5: run_maintenance_bg() and tell user:
         "Vault has N notes flagged for repair — healing in background."
       (Do this only once per session, even if the count stays high.)

2. search_vault("<what the user just asked about>")
   → Before responding to the first substantive question, check the vault.
   → If relevant results exist, incorporate them into your answer and cite the source titles.
```

### Session-end ritual (trigger on any goodbye/done signal)

```
export_session(
  title="<what was accomplished this session>",
  summary="<2-4 sentences covering discussion, decisions, and output>",
  key_decisions="<decision 1>, <artifact created>, <next step agreed>"
)
```

Confirm briefly with "Session saved to Sessions/" — nothing else.  
The raw transcript is saved automatically by the Claude Code hook (if configured).

---

## Cross-surface memory bridge (Claude Desktop/Cowork ↔ Claude Code)

Claude Code cannot read Claude Desktop's Chat or Cowork conversation history, and vice versa —
the two surfaces have completely separate session storage. `delegation-core` is the bridge:
register this MCP server in **both** `claude_desktop_config.json` and `~/.claude.json`, and all
surfaces read and write the same vault.

How it works in practice:

- **Desktop/Cowork → vault**: the standing orders above (`export_session()` on goodbye,
  `write_note()` for Decisions/Fixes/Reference as they happen) mean every Desktop or Cowork
  session leaves a curated digest in the vault — not just a wall of raw chat log.
- **vault → Code**: a `SessionStart` hook (`hooks/session_start_brief.py`, stdlib-only) prints a
  short "what changed since you were last here" brief at the start of every Claude Code session
  — listing notes added/updated since the last Code session, plus an `_inbox/` count if files
  are waiting. This is injected directly into context, so Code is aware of Desktop/Cowork output
  without needing raw transcript access. If `_inbox/` is non-empty and maintenance hasn't fired
  in the last 30 minutes, the hook also launches `delegation-core maintain` in the background
  (logs to `~/.delegation_core/maintenance.log`) — files dropped from either surface get
  classified, deduped, and filed without anyone needing to remember to call `run_maintenance`.
- **Code → vault**: the existing `SessionEnd` hook (`hooks/session_export.py`) backs up the raw
  Code transcript to `Sessions/`, and `export_session()` writes the curated digest — both
  readable from Desktop/Cowork via `search_vault()`/`read_note()`. After writing the
  transcript, the hook also fires a detached `delegation-core reindex` (logs to
  `~/.delegation_core/reindex.log`) so the transcript is searchable immediately, without
  waiting for the next `vault_reindex_bg()`.

Net effect: decisions made in a Desktop chat are visible to Code on its next session, and
findings from a Code session are searchable from Desktop/Cowork — without any direct API
access between the two surfaces.

---

## Session startup ritual

Run these at the beginning of every session, silently:

```
1. heartbeat()
   → If degraded: notify the user. Do not proceed with vault operations.
   → If healthy: continue.

2. search_vault("<what the user just asked about>")
   → Before responding to the first substantive question, check the vault.
   → If relevant results exist, incorporate them into your answer and cite the source titles.
```

Do not announce that you are doing these steps unless the user asks.

---

## Chaining patterns

### Pattern 1 — Research and record

User shares a document or link content and asks you to analyze it.

```
compress(source="<doc name>", raw_content="<paste>")
  → reason over compressed result
  → write_note(folder="Reference", title="<topic>", content=<your analysis + compressed facts>)
  → search_vault("<topic>") to find related prior notes
  → surface connections to the user
```

### Pattern 2 — Decision logging

User makes or confirms a decision during conversation.

```
write_note(
  folder="Decisions",
  title="<decision topic> — <date>",
  content="## Decision\n<what was decided>\n\n## Rationale\n<why>\n\n## Next steps\n<actions>"
)
```

Do this immediately when the decision is reached, not at the end of the session.

### Pattern 3 — Context recall

User asks about something that might have been discussed before.

```
search_vault("<user's question as a query>")
  → If summary contains relevant context: answer using it, cite source titles
  → If no results: answer from your own knowledge, note that nothing is in the vault
  → If the answer turns out to be new and useful: write_note to persist it
```

### Pattern 4 — Meeting debrief

User wants to log a meeting or conversation.

```
compress(source="meeting notes", raw_content="<raw notes>")
  → write_note(folder="Sessions", title="<meeting title> <date>", content=<compressed summary>)
  → search_vault("<meeting topic>") to find related decisions or prior context
  → surface any conflicts or continuations to the user
```

### Pattern 5 — Inbox processing (small)

User has a handful of files to organize.

```
Tell user: "Drop your files into <vault>/_inbox/ and let me know when they're there."
  → vault_inbox_status()  ← confirm files are there and how many
  → run_maintenance()     ← wait for result
  → report classified, merged, and any errors
  → search_vault("<topic of the files>") to show what is now findable
```

### Pattern 6 — Inbox processing (large / fire-and-forget)

User has many files or wants the conversation to continue while processing runs.

```
vault_inbox_status()     ← confirm count and file names
  → run_maintenance_bg() ← returns job_id immediately
  → tell user: "Processing N files in background — I'll check back."
  → [continue conversation or wait for user signal]
  → task_status(job_id)  ← poll until status == "done"
  → report results from job.result
```

### Pattern 7 — Updating an existing record

User provides new information about something already in the vault.

```
search_vault("<topic>")               ← find the existing note
  → vault_update_note("<note_name>", "<new content>")
  → confirm what was appended and to which note
```

Do NOT create a new note for follow-up information. Append to the original.

### Pattern 8 — Duplicate check before writing

Before creating a note on a topic, check if one already exists.

```
search_vault("<topic>")        ← quick check
  if results.similarity > 0.85:
    vault_find_similar("<closest result title>", threshold=0.85)
      → if near-duplicate found: vault_update_note() instead of write_note()
      → if sufficiently different: write_note() and note the relationship
```

---

## Content guidelines for `write_note`

Notes written to the vault should be searchable by future queries. Structure them so
that BGE embeddings can match them semantically.

**Do not write a `title:` / `date:` / `ai_generated:` frontmatter block.**
`write_note` generates those three from the `title` argument. Repeating them in
`content` used to produce a note with two stacked frontmatter blocks — Obsidian
parses only the first, so everything in the second rendered as literal body text
after a horizontal rule. The blocks are merged now rather than stacked, but the
duplication is still noise. Start `content` at the first heading.

**Good note structure:**
```markdown
## Summary
<2-3 sentence overview>

## Key facts / decisions
- <bullet points>

## Context
<why this matters, what led to it>

## Next steps
- <actionable items if any>
```

**When you do need frontmatter**, include a block with *only* the extra keys.
It is preserved verbatim — block lists included — and the generated keys are
added underneath only where you did not supply them:

```markdown
---
subtitle: "the long descriptive version, for humans skimming the note"
aliases:
  - Alternate Name
---

## Summary
...
```

A `title:` you supply wins over the generated one. That is how a note carries a
short display title while its filename still comes from the `title` argument —
useful because the filename is what the dashboard labels graph nodes
with, and `safe_filename` truncates it at 50 characters.

Avoid vague titles like "Meeting notes" or "Research". Use specific titles like
"Decision: Migrate auth service to OAuth2 — 2026-06-03" or
"Research: Competitor pricing analysis Q2 2026".

---

## Error handling

| Error | Action |
|-------|--------|
| `heartbeat` returns `degraded` | Warn user: "The local AI engine is starting up — first tool call may take up to 90 seconds." Then retry. |
| `search_vault` returns no results | Proceed without vault context. Do not invent vault content. |
| `write_note` returns invalid folder error | Call `heartbeat()` to get valid folder list, retry with correct folder. |
| `compress` times out | The model is under load. Wait 30 seconds and retry once. |
| Any tool returns `{"error": "..."}` | Report the error to the user. Do not silently ignore it. |

---

## Limits

| Parameter | Limit |
|-----------|-------|
| `compress` input | 6000 characters per call — chunk larger content |
| `search_vault` limit | Default 5, max recommended 10 — higher limits slow response |
| `write_note` content | No hard limit, but keep notes focused — one topic per note |
| `read_note` | Returns full raw markdown — prefer `search_vault` for most lookups |

---

## Process tracking

Processes are persistent, cross-session work items stored in `~/.delegation_core/processes.json`.
They survive server restarts. Any AI that connects will see the same process state.

### `process_create(name, description="", steps="")`
Start tracking a task that will take multiple conversations or require follow-up.
`steps` is a comma-separated string: `"Gather data, Analyse, Draft report, Review"`.

```json
→ { "id": "proc_a1b2c3", "name": "Q3 Budget Review", "status": "active",
    "steps": [{"index": 0, "description": "Gather data", "done": false}, ...],
    "notes": [], "created": "2026-06-03T14:00:00" }
```

**Create a process when:** the task has more than one step, will take more than one conversation,
involves follow-up actions, or the user says "remind me", "we need to", "don't forget to".

---

### `process_list(status="active", query="")`
List processes. Called automatically via `heartbeat()` at session start (top 5 active shown).
Use `query` to filter by keyword across name, description, and notes.

```json
→ { "count": 2, "processes": [
    { "id": "proc_a1b2c3", "name": "Q3 Budget Review", "steps_done": "1/4",
      "last_note": "Waiting on IT submission", "updated": "2026-06-03" },
    { "id": "proc_d4e5f6", "name": "Vendor Evaluation", "steps_done": "0/3",
      "last_note": "", "updated": "2026-06-02" }
  ]}
```

---

### `process_update(process_id, note="", step_done=-1, status="")`
Update progress on a tracked process. All parameters are optional.
`step_done` is the zero-based index of a step to mark complete.
`status`: `"active"` | `"paused"` | `"done"` | `"cancelled"`.

```json
// Mark step 0 done and add a note:
process_update("proc_a1b2c3", note="IT submitted budget", step_done=0)

// Pause a process:
process_update("proc_a1b2c3", status="paused")

// Complete a process:
process_update("proc_a1b2c3", status="done")
```

Call `process_update` immediately when: a step is completed, new information arrives,
status changes, or the user says something relevant to an active process.

---

### `process_get(process_id)`
Full detail view: all steps with completion timestamps, all notes in order, full history.
Use before resuming work on a process to recall exactly where things stand.

```json
→ { "id": "proc_a1b2c3", "name": "Q3 Budget Review", "status": "active",
    "steps": [
      {"index": 0, "description": "Gather data", "done": true, "completed_at": "2026-06-03T14:22:00"},
      {"index": 1, "description": "Analyse", "done": false, "completed_at": null}
    ],
    "notes": [
      {"text": "IT submitted budget", "timestamp": "2026-06-03T14:22:00"},
      {"text": "Finance needs 2 more days", "timestamp": "2026-06-03T16:10:00"}
    ] }
```

---

### Process tracking patterns

**Starting a project:**
```
User: "We need to evaluate three vendors for the new CRM."
→ process_create("CRM Vendor Evaluation",
                 "Compare three vendors and make a recommendation",
                 "Define criteria, Evaluate Vendor A, Evaluate Vendor B, Evaluate Vendor C, Compare and decide")
→ write_note(folder="Decisions", ...) if criteria are agreed upon
```

**Resuming across sessions:**
```
Session start → heartbeat() surfaces: "1 active process: CRM Vendor Evaluation (1/5 steps)"
→ process_get("proc_a1b2c3") to recall full state
→ search_vault("CRM vendor") to load related notes
→ brief the user: "Last time we defined the criteria. Ready to evaluate Vendor A?"
```

**Capturing updates mid-conversation:**
```
User: "Vendor A sent their proposal — looks promising."
→ process_update("proc_a1b2c3", note="Vendor A proposal received — initial impression positive")
→ compress() the proposal if shared
→ write_note(folder="Reference", ...) to persist the analysis
```

---

## Supported file formats

Drop any of these into `<vault>/_inbox/` and run `run_maintenance` or `run_maintenance_bg`:

| Format | Extensions | Notes |
|--------|-----------|-------|
| Markdown | `.md`, `.markdown`, `.mdx` | Native format — read directly. MDX keeps its prose; JSX components stay as inert tags |
| Plain text | `.txt`, `.text` | Read directly |
| CSV | `.csv` | Converted to markdown table |
| HTML | `.html`, `.htm` | Tags stripped, text extracted |
| PDF | `.pdf` | Text-based PDFs only. Scanned PDFs produce a stub note — tell the user to export text manually |
| Word | `.docx` | Paragraphs and tables extracted |
| Excel | `.xlsx` | Each sheet converted to markdown table (max 200 rows) |
| PowerPoint | `.pptx` | Slide text extracted in order |
| JSON | `.json` | Supported by `extractor.py` — this row was missing from earlier versions of this file, so file counts made from the list above ran low |

**Watch for data files masquerading as documents.** The extractor accepts `.csv` and `.json`
whatever they contain, and a large data file becomes many chunks and many embeddings without
answering any question. On 2026-09-03 a single `google-fonts.csv` (745 KB) plus two
`country-meta.json` (340 KB each) generated roughly 450 BGE embeddings and dominated a
45-minute ingest. Pass `exclude` for `data`/`assets` directories when ingesting a folder that
ships reference datasets.

**Images are not supported.** If the user wants to store an image reference, ask them to drop a `.txt` file describing it instead.

**If `vault_inbox_status()` returns files in the `unsupported` list**, tell the user which files cannot be processed and what format to convert them to.

**Boilerplate files are auto-skipped.** `run_maintenance`/`run_maintenance_bg` filter out
license files, READMEs, `CHANGELOG`/`NOTICE`/`CONTRIBUTING`, `requirements*.txt`, and similar
boilerplate before classification — these are moved to `_processed/` and listed under the
`junk` key in the result, not filed as notes. Session logs/transcripts are routed straight to
`Sessions/` (by filename or content signal) and are never merged into an existing note, to
avoid several unrelated sessions piling into one shared note.

**Scanned PDFs** return a stub note with the filename and page count. Tell the user: *"This PDF appears to be scanned — I've logged it but cannot extract the text. Export the text from Adobe or Google Docs and drop the .txt file in the inbox."*

---

## What delegation-core is not

- It is not a calculator or code executor.
- It is not a replacement for your reasoning — it handles retrieval and compression,
  you handle judgment and synthesis.
- The local model (llama.cpp) is efficient but not as capable as you.
  Use `compress` and `search_vault` to preprocess, then apply your own reasoning to the result.

---

## Quick reference

```
Session start            → capabilities() + heartbeat() + search_vault(<first topic>)
Session end (any goodbye)→ export_session(<title>, <summary>, <key_decisions>)
Multi-step task begins   → process_create(<name>, <description>, <steps>)
Step completed           → process_update(<id>, step_done=<index>)
New info on a process    → process_update(<id>, note=<text>)
Resume a process         → process_get(<id>) + search_vault(<topic>)
Finish a process         → process_update(<id>, status="done")
List what's in progress  → process_list()
Got a document           → compress() before reading
Made a decision          → write_note(folder="Decisions", ...)
Did research             → write_note(folder="Reference", ...)
Had a meeting            → write_note(folder="Sessions", ...)
Found a fix              → write_note(folder="Fixes", ...)
Adding to existing note  → vault_update_note(<name>, <new content>)
User asks recall         → search_vault(<query>)   [escopo adaptativo; veja default_search_scope]
Pergunta sobre um cliente→ search_vault(<query>, client=<nome>)   [combina com scope]
Que clientes existem     → vault_stats().clients
Question about a codebase→ search_vault(<query>, graph=<name>) ou scope='generated'
Rename a note            → vault_rename_note(<path>, <new title>)  (repoints links)
Browse a folder          → vault_list_notes(<folder>)
Check for duplicates     → vault_find_similar(<note_name>)
Files to sort (small)    → vault_inbox_status() → run_maintenance()
Files to sort (large)    → vault_inbox_status() → run_maintenance_bg() → task_status(<id>)
Bulk import done         → vault_reindex_bg() → task_status(<id>)
External folder (small)  → ingest_folder(<path>)
External folder (large)  → ingest_folder_bg(<path>) → task_status(<id>)
Check ingest status      → ingest_status()
Search web               → search_web(<query>) → write_note if useful
Cross-link a folder      → relink_folder(<folder>)   [pequena]
Cross-link a folder      → relink_folder_bg(<folder>) → task_status(<id>)   [grande]
Vault repair backlog     → vault_health.needs_repair > 5 → run_maintenance_bg()
Which links are broken   → vault_health_detail()   [nunca conte por script]
Graph a codebase         → graph_preview(<path>, exclude=[...]) → graph_build_bg(same exclude)
Re-export a built graph  → graph_export(<name>, "graphml"|"svg"|"cypher")
What can this server do  → capabilities()
Check job progress       → task_status(<job_id>) → wait check_again_in_seconds
Check vault size         → vault_stats()
Check what's connected   → list_mcp_clients()
```
