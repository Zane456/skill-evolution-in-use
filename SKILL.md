---
name: skill-evolution-in-use
description: Makes a chosen skill evolve from REAL use — logs each session that invokes it under the skill's own `evolve/` directory (hook script, no model), stores the skill's changes as numbered versions, and restores any version on request. ALWAYS invoke when the user wants a skill to learn from being used, or manages that - "给 X 开启进化", "让这个 skill 使用中进化", "记录 X 的真实使用", "X 进化状态/攒了多少记录", "X 退回 v2", "回滚 skill 版本", "锁定/解锁 X", "关闭 X 的进化", "enable evolution for X", "let this skill learn from usage", "roll the skill back" - even when they only say "开启进化" or "退回上一版" about a skill. For a skill with evolution enabled, do not hand-roll its backups, usage logs or rollbacks - use this skill first; prefer it over skills-evolve whenever the evidence is real usage, not test scenarios. Do NOT use for building a test lab with scenarios (skills-evolve), a one-off audit or restructure of a skill (skill-checkup), writing a new skill (skill-creator), or plain edits to a skill's text.
---

# skill-evolution-in-use — let a skill learn from being used

Give one chosen skill (the *target*) a memory of how it is really used. Every
conversation that invokes it is copied into the target's own folder, its
changes are stored as numbered versions, and any version can be restored.
Real use shows what test prompts cannot: what the user corrects.

`scripts/evo.py` does all of it (its logic lives in `scripts/evo_core.py`).
Run it instead of redoing its steps by hand: it keeps state, versions and
changelog consistent, and a hand edit under `evolve/` breaks rollback.

```bash
python3 "<this-skill-dir>/scripts/evo.py" <command> [<skill>]
```

`<skill>` is a folder name under the skills root, the skill's `name`, or a path.
Exit 0 = done. Exit 2 = refused or failed, also for bad arguments, with the
reason printed: relay it, don't work around it. Exit 3: see Enabling. If the
skill was never enabled (exit 2 says so), offer `enable`; never enable on your
own initiative.

## What the user wants → command

| User intent | Command |
|---|---|
| turn it on ("给 X 开启进化", "let X evolve in use") | `enable <skill>` |
| how is it going, what was recorded | `status [<skill>]` |
| which versions exist | `versions <skill>` |
| go back to a version ("X 退回 v2") | `rollback <skill> v<N>` |
| undo the last change ("退回上一版") | `rollback <skill> prev` |
| keep a checkpoint now | `snapshot <skill> --note "<why>"` |
| freeze / unfreeze the skill | `lock <skill>` / `unlock <skill>` |
| stop recording | `disable <skill>` |
| take the recorder out of Claude Code | `uninstall-hooks` |

## Enabling

1. Run `enable <skill>`.
2. Exit 3 = `skill-checkup` is missing (later phases check edits with it).
   Tell the user it will be fetched from GitHub, run `install-checkup` with a
   timeout of several minutes (slow links take over a minute), then `enable`
   again. If the fetch fails, tell the user and run
   `enable <skill> --skip-checkup`: nothing in this release uses skill-checkup.
3. Account-synced and plugin skills are refused (exit 2): their updater
   overwrites local changes. Offer a copy in the user's own skills folder.
4. Tell the user the stored version, the data folder, and when recording
   starts, as `enable` prints it: hooks just installed → the next Claude Code
   session (hooks load at session start); hooks already installed → the
   skill's next invocation, also in sessions already open. Earlier use is not
   recorded.

Enabling does not modify the target's own files. It adds `<skill>/evolve/`
(its `.gitignore` keeps records out of any git repo) and, once per machine,
two hook entries (Stop, SessionEnd) to the Claude Code `settings.json`, with a
backup beside it.

## What is recorded

From the turn that first invokes the target (Skill tool or `/name`) until the
window closes: user and assistant text verbatim, one line per tool call, tool
errors and warning lines. Other tool output is dropped: it is bulk, not
evidence. One window is one record, including after `/clear` once the target
is invoked again.

Before reading or quoting a record, read `references/record-format.md`.

Records are real conversations. Keep them on the machine, and leave deletion
to the user.

## Versions and rollback

- A version is a full copy of the target. Files over 2 MB are tracked by size,
  not copied.
- An edit made outside this tool is stored as a version (actor `external`)
  the next time the target is used, so every record points at content that
  really existed. Edits made between two uses become one version.
- Version numbers only go up. `rollback demo v2` stores unsaved edits first,
  restores the content of v2, and saves the result as a new version. Report
  the new number and the undo command it prints.
- Run a rollback when the user asks for one without a second confirmation.
  Only a file over 2 MB is beyond undo, so a rollback never overwrites or
  deletes one: it keeps it and warns. Relay those warnings.

## Lock

`lock` means no automatic edits (later phases obey it); recording goes on. It
is a flag, not file protection. A rollback the user asks for is allowed on a
locked skill; for any other edit through this skill, leave the files alone,
say it is locked, and point to `unlock`.

## Not in this release

Improving the target from its records (summaries, automatic fixes,
user-approved preference changes) comes in later phases. Asked to "evolve X
now" / "改吧": say this release records and versions only, show `status`, and
stop. Improvised edits from records skip those phases' safeguards (evidence
counts, independent review, replay of past corrections), and one task's
special request easily becomes a standing rule.

## Reporting

After a command, tell the user, in their language, the skill, version, what
changed and what happens next. Leave out the commands themselves and any
file listing beyond the data folder. If `status` shows
hook errors, say so: recording may be failing silently.
