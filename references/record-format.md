# Record format

Where: `<skill>/evolve/records/<YYYYMMDD-HHMMSS>-<session8>.md`, with a
sidecar `<same name>.json`.

## The Markdown file

Append-only. Written by `scripts/record_hook.py` after each assistant reply.

```
# Record · <skill>
- record / skill · v<N> / started / project / session / entrypoint / transcript

## Turn <n> · <MM-DD HH:MM>
**User**
> the user's message, verbatim
**Claude did**
- <Tool> · <one-line summary> [◆skill-file] [❌ + error text]
  ⚠ <warning line>
**User** _(sent while Claude was working)_
> a message the user typed mid-turn
**Claude said**
> the assistant's text, verbatim
```

- `entrypoint` = how Claude Code was started, from the transcript: `cli` is an
  interactive window; `sdk-*` (e.g. `sdk-cli` = `claude -p`, often a worker
  dispatched by another agent) is not. In a non-interactive session the turn
  header says `**Prompt (non-interactive)**` instead of `**User**`: nobody
  typed it.
- A turn starts at a user prompt or a message that begins with a `/command`.
  Built-in commands (`/clear`, `/model`, …) are not turns. A `/command` that
  is the very last thing in a session, with no reply after it, is not
  recorded.
- `**User** _(sent while Claude was working)_` — typed while a turn was
  running; part of that turn, not a new one. A message a plugin relays as
  the user's (e.g. typed in a side pane) is also under **User**.
- Tool line summary: Bash uses its `description`; file tools use the path;
  others use their first meaningful argument, cut at 140 chars. A Skill call
  keeps its arguments up to 600 chars: they are the instruction the skill got.
- `◆skill-file` = the call names a path inside the target skill folder (also
  written as `~/…` or `$HOME/…`); `evolve/` and sibling folders do not count.
- `❌` = the tool returned an error. The error text follows, cut at 2000 chars.
- `⚠` = a successful result that still printed a warning, an error word, or
  "not found" / "no … found": up to 3 such lines, each cut at 200 chars. Read
  results are file content, so they are not scanned. All other tool output is
  dropped.
- A message over 20000 chars is cut, with a note. The full text stays in the
  transcript named in the header (Claude Code deletes transcripts after its
  retention period, 30 days by default).
- `> [new session in the same window · …]` — the window ran `/clear` (or
  otherwise started a new session) and invoked the skill again.
- `> [skill version: vA → vB]` — the target's content changed mid-record.
- `- _(user interrupted)_` — the user stopped the assistant mid-turn.
- `- _(background task: …)_` / `- _(message from another session …)_` — a
  message Claude Code wrote itself, known by the entry's origin fields or a tag
  at the very start of the message (a user quoting a tag is still the user).
  It is not the user speaking and does not start a turn; only its one-line
  summary is kept.

Not recorded: sub-agent conversations, system reminders, hook output, the
injected skill body, thinking.

## The sidecar JSON

`record`, `skill`, `version` (last seen), `turns`, `sessions`, `window`,
`project`, `started`, `updated`, `tidied_turns` (how many turns a later
analysis phase has already processed; 0 in this release).

## Other files under `evolve/`

- `.gitignore` — `*`, so no git repo around the skill picks up records.
- `state.json` — enabled, locked, current version, content hash, `enabled_at`
  (use before it is not recorded).
- `versions/v<N>/` — full copy of the skill at that version plus
  `manifest.json` (actor, note, per-file hashes). A stored `SKILL.md` is named
  `SKILL.md.snapshot` so it is never loaded as a skill. A version is built in
  a hidden temp folder and appears only when complete; leftovers of an
  interrupted run are moved aside as `.v<N>.debris-*`, never deleted.
- `changelog.md` — one entry per version: when, who (`enable`, `external` =
  edited outside this tool, `manual` = stored on request, `rollback`, later
  `background`, `user-approved`), which files changed.
