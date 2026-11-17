#!/usr/bin/env python3
"""Stop / SessionEnd hook: copy the conversation into each enabled skill's record.

No model is involved. Only the transcript bytes added since the last run are
read. This script must never disturb Claude Code: it prints nothing, every
failure goes to a log file, and the exit code is always 0.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evo_core as core  # noqa: E402

MAX_TEXT = 20000      # one message; longer ones are cut with a note
MAX_ERR = 2000        # one tool error
MAX_WARN = 200        # one warning line kept from a successful tool result
WARN_LINES = 3
SESSION_LOCK_WAIT = 2.0
CMD_RE = re.compile(r"<command-name>/?([\w.:一-鿿-]+)</command-name>")
CMD_START_RE = re.compile(r"\s*<command-(?:name|message)>")
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
WARN_RE = re.compile(r"(?i)\b(warn(ing)?|error|fail(ed|ure)?|not found|no .{0,40} found|traceback|denied|refused|exception)\b")
ENTRYPOINT_RE = re.compile(rb'"entrypoint":\s*"([^"]*)"')
STDOUT_TAGS = ("<local-command-stdout>", "<local-command-stderr>")
NO_WARN_TOOLS = {"Read"}      # its result is file content, not a status
TYPE_MARKS = (b'"type":"user"', b'"type":"assistant"', b'"type": "user"', b'"type": "assistant"',
              b'"queued_command"', b'"local_command"', b'"queue-operation"')

HEADER = """# Record · {skill}

- record: {rid}
- skill: {skill} · v{ver}
- started: {started}
- project: {cwd}
- session: {sid}
- entrypoint: {ep}
- transcript: {tp}

> Trimmed copy of a real conversation. User and assistant text is verbatim (a message over {max} chars is cut); each tool call is one line; tool output is dropped except errors and warning lines. Written by a script, no model involved.
"""


def log(msg: str) -> None:
    with contextlib.suppress(Exception):
        rd = core.run_dir()
        rd.mkdir(parents=True, exist_ok=True)
        lp = rd / "hook.log"
        if lp.exists() and lp.stat().st_size > 512 * 1024:
            lp.write_text("", encoding="utf-8")
        with open(lp, "a", encoding="utf-8") as f:
            f.write(f"{core.now_iso()} {msg}\n")


# ---------------------------------------------------------------- transcript -> events

def clean(text: str) -> str:
    return REMINDER_RE.sub("", text or "").strip()


def flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _lead(text: str) -> str:
    """The message start, also when Claude Code wrapped a notification in a reminder."""
    s = text.lstrip()
    if s.startswith("<system-reminder>"):
        s = s[len("<system-reminder>"):].lstrip()
        if s.startswith("[SYSTEM NOTIFICATION"):
            s = s.split("\n", 1)[1].lstrip() if "\n" in s else ""
    return s


def _system_event(text: str, d: dict):
    """Claude Code also writes its own messages as `user` entries: background-task
    notifications, messages from other sessions. They are not the user's words, so
    they must never be recorded under **User**. Returns a one-line label, or None.
    Only structural fields or a tag at the very start count: a person may quote a tag."""
    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
    if origin.get("asUser"):
        return None                      # typed by the person, relayed by a plugin
    kind = origin.get("kind")
    lead = _lead(text)
    is_task = (kind == "task-notification" or d.get("turnOrigin") == "task_notification"
               or lead.startswith("<task-notification"))
    is_peer = (kind == "peer" or d.get("turnOrigin") == "peer"
               or lead.startswith(("<teammate-message", "<agent-message", "Another Claude session sent a message")))
    flagged = (d.get("promptSource") == "system" or (kind and kind != "human")
               or d.get("turnOrigin") not in (None, "human", "sdk"))
    if not (is_task or is_peer or flagged):
        return None
    if is_task:
        m = re.search(r"<summary>(.*?)</summary>", text, re.S)
        return "background task: " + one_line(m.group(1) if m else "notification", 160)
    if is_peer:
        ids = list(dict.fromkeys(re.findall(r'teammate_id="([^"]+)"', text)))
        m = re.search(r'summary="([^"]*)"', text)
        return ("message from another session" + (f" ({', '.join(ids)})" if ids else "")
                + (": " + one_line(m.group(1), 160) if m else ""))
    return "system message: " + one_line(clean(text) or "(no text)", 160)


def _user_text_event(text: str, ts, d: dict):
    sysev = _system_event(text, d)
    if sysev:
        return ("event", sysev, ts)
    m = CMD_START_RE.match(text) and CMD_RE.search(text)
    if m:
        a = ARGS_RE.search(text)
        return ("slash", m.group(1), a.group(1).strip() if a else "", ts)
    text = clean(text)
    if not text:
        return None
    if text.startswith("[Request interrupted"):
        return ("interrupt", text, ts)
    return ("prompt", text, ts)


def _content_text(c) -> str:
    """Text of a message body given as a string or as content blocks; images become [image]."""
    if isinstance(c, str):
        return c
    texts = []
    for b in c if isinstance(c, list) else []:
        if isinstance(b, dict) and b.get("type") == "text":
            texts.append(b.get("text") or "")
        elif isinstance(b, dict) and b.get("type") == "image":
            texts.append("[image]")
    return "\n".join(texts)


def _typed_mid_turn(a: dict) -> bool:
    origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
    return a.get("type") == "queued_command" and (origin.get("kind") == "human" or bool(origin.get("asUser")))


def _norm(text: str) -> str:
    return " ".join(clean(text).split())[:60]


def _queued_event(text: str, ts):
    """A prompt delivered from the input queue: a /command or plain text."""
    if text.startswith("/"):
        name, _, args = text[1:].partition(" ")
        if name and CMD_RE.fullmatch(f"<command-name>/{name}</command-name>"):
            return ("slash", name, args.strip(), ts)
    return ("prompt", text, ts)


def parse(lines, queue=None):
    """Turn raw transcript lines into (events, tool_results, event_line_numbers).

    `queue` carries messages the person typed while Claude was working, between calls
    (a list the caller keeps). Some Claude Code versions store such a message ONLY as a
    `queue-operation` entry: enqueue (typed) -> dequeue (delivered as the next prompt).

    events: ("prompt", text, ts) | ("slash", name, args, ts) | ("said", text, ts)
            | ("tool", id, name, input, ts) | ("interrupt", text, ts) | ("event", label, ts)
            | ("queued", text, ts)  (the person wrote while Claude was working)
    The transcript also carries request-level copies of the same messages under
    other `type` values; only `user` / `assistant` entries are the conversation,
    plus `attachment` entries holding a message the person sent mid-turn and
    `system` local_command entries marking a built-in command.
    """
    events, results, ev_lines = [], {}, []
    queue = [] if queue is None else queue
    delivered = [None]       # text just delivered from the queue: skip its copy as a user entry

    def add(ev, n):
        if ev[0] in ("prompt", "slash", "queued") and delivered[0] is not None:
            same = _norm(ev[1] if ev[0] != "slash" else "/" + ev[1] + (" " + ev[2] if ev[2] else "")) == delivered[0]
            delivered[0] = None
            if same:
                return
        events.append(ev)
        ev_lines.append(n)

    def builtin_marker():
        # the command just before it was a built-in (/clear, /model...), not a prompt
        if events and events[-1][0] == "slash":
            events.pop()
            ev_lines.pop()

    for n, raw in enumerate(lines):
        if not any(m in raw for m in TYPE_MARKS):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        if d.get("isSidechain"):
            continue
        if t == "queue-operation":
            op, c = d.get("operation"), d.get("content")
            if op == "enqueue" and isinstance(c, str) and clean(c) and not _system_event(c, {}):
                queue.append([clean(c), d.get("timestamp")])
            elif op == "remove" and isinstance(c, str):
                # absorbed mid-turn: recorded through its queued_command attachment instead
                for i, (q, _ts) in enumerate(queue):
                    if _norm(q) == _norm(c):
                        del queue[i]
                        break
            elif op == "dequeue" and queue:
                text, qts = queue.pop(0)
                add(_queued_event(text, d.get("timestamp") or qts), n)
                delivered[0] = _norm(text)
            continue
        if t == "system":
            c = d.get("content")
            if d.get("subtype") == "local_command" and isinstance(c, str) and c.lstrip().startswith(STDOUT_TAGS):
                builtin_marker()
            continue
        if t == "attachment":
            a = d.get("attachment") if isinstance(d.get("attachment"), dict) else {}
            if _typed_mid_turn(a):
                text = clean(_content_text(a.get("prompt")))
                if text:
                    add(("queued", text, d.get("timestamp") or a.get("timestamp")), n)
            continue
        if t not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        c = msg.get("content")
        ts = d.get("timestamp")
        meta = bool(d.get("isMeta")) or bool(d.get("isCompactSummary"))
        if t == "user":
            if isinstance(c, str):
                if c.lstrip().startswith(STDOUT_TAGS):
                    builtin_marker()
                    continue
                if meta:
                    continue
                ev = _user_text_event(c, ts, d)
                if ev:
                    add(ev, n)
            elif isinstance(c, list):
                texts = []
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "tool_result":
                        results[b.get("tool_use_id")] = (bool(b.get("is_error")), flatten(b.get("content")))
                    elif bt == "text":
                        texts.append(b.get("text") or "")
                    elif bt == "image":
                        texts.append("[image]")
                if meta or not texts:
                    continue
                ev = _user_text_event("\n".join(texts), ts, d)
                if ev:
                    add(ev, n)
        else:
            if isinstance(c, str):
                if c.strip():
                    add(("said", c, ts), n)
                continue
            for b in c or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and (b.get("text") or "").strip():
                    add(("said", b["text"], ts), n)
                elif bt == "tool_use":
                    add(("tool", b.get("id"), b.get("name") or "?", b.get("input") or {}, ts), n)
    return events, results, ev_lines


# ---------------------------------------------------------------- rendering

def when(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return "--"


def cap(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + f"\n… [{len(text) - MAX_TEXT} more chars not copied; the full text is in the transcript]"


def quote(text: str) -> str:
    return "\n".join(("> " + ln) if ln else ">" for ln in text.split("\n"))


def short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def one_line(s, n: int = 140) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_summary(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return one_line(inp.get("description") or inp.get("command") or "")
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return short_path(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if name == "Skill":
        return one_line(f"{inp.get('skill', '')} {inp.get('args') or ''}".strip(), 100)
    for k in ("description", "pattern", "query", "url", "prompt", "path", "command"):
        if isinstance(inp.get(k), str) and inp[k].strip():
            return one_line(short_path(inp[k]))
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return one_line(short_path(v))
    return ""


def touches_skill(inp: dict, sd: Path) -> bool:
    """A path inside the skill folder (not a sibling sharing its prefix, not evolve/),
    also when a command writes it as ~/... or $HOME/..."""
    if not isinstance(inp, dict):
        return False
    home = str(Path.home())
    stop = r"""(?:$|[\s"'`;|&)])"""      # what may follow a path in a command
    pats = [re.compile(re.escape(r) + r"(?:/(?!" + core.EVOLVE + r"(?:/|" + stop + r"))|(?=" + stop + r"))")
            for r in {str(sd), str(core.skills_root() / sd.name)}]
    for v in inp.values():
        if not isinstance(v, str):
            continue
        v = v.replace("${HOME}", home).replace("$HOME", home)
        v = re.sub(r"""(?<![^\s"'=:(])~(?=/)""", lambda _m: home, v)
        if any(p.search(v) for p in pats):
            return True
    return False


def fence(text: str) -> str:
    bar = "````" if "```" in text else "```"
    return f"{bar}text\n{text}\n{bar}"


def warn_lines(text: str) -> list:
    """Lines of a successful tool result that say something went wrong or was not found."""
    out = []
    for ln in (text or "").splitlines():
        if WARN_RE.search(ln):
            out.append(ln.strip()[:MAX_WARN])
            if len(out) == WARN_LINES:
                break
    return out


def tool_line(ev, results, sd: Path) -> str:
    _, tid, name, inp, _ts = ev
    line = f"- {name} · {tool_summary(name, inp)}"
    if touches_skill(inp, sd):
        line += " ◆skill-file"
    r = results.get(tid)
    if r and r[0]:
        err = (r[1] or "").strip()
        if len(err) > MAX_ERR:
            err = err[:MAX_ERR] + f"\n… [{len(err) - MAX_ERR} more chars dropped]"
        block = "\n".join("  " + ln for ln in fence(err).split("\n"))
        return f"{line} ❌\n\n{block}\n\n"
    if r and name not in NO_WARN_TOOLS:
        line += "".join(f"\n  ⚠ {w}" for w in warn_lines(r[1]))
    return line + "\n"


def render(evs, results, sd: Path, turn: int, who: str = "**User**"):
    out, last = [], None
    for ev in evs:
        kind = ev[0]
        if kind in ("prompt", "slash"):
            turn += 1
            text = ev[1] if kind == "prompt" else ("/" + ev[1] + (" " + ev[2] if ev[2] else ""))
            out.append(f"\n## Turn {turn} · {when(ev[-1])}\n\n{who}\n\n{quote(cap(text))}\n")
        elif kind == "queued":
            out.append(f"\n{who} _(sent while Claude was working)_\n\n{quote(cap(ev[1]))}\n")
        elif kind == "said":
            out.append(f"\n**Claude said**\n\n{quote(cap(ev[1]))}\n")
        elif kind == "tool":
            if last != "tool":
                out.append("\n**Claude did**\n\n")
            out.append(tool_line(ev, results, sd))
        elif kind == "interrupt":
            out.append("\n- _(user interrupted)_\n")
        elif kind == "event":
            out.append(f"\n- _({ev[1]})_\n")
        last = kind
    return "".join(out), turn


# ---------------------------------------------------------------- windows, records

def window_key(sid: str) -> str:
    """One Claude Code window = one process, which survives /clear. Falls back
    to the session id when that process cannot be found."""
    k = os.environ.get("SKILL_EVOLUTION_WINDOW_KEY")
    if k:
        return k
    pid = os.getppid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,lstart=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except Exception:
            break
        parts = out.split(None, 6)
        if len(parts) < 7:
            break
        ppid, lstart, comm = parts[0], " ".join(parts[1:6]), parts[6]
        if os.path.basename(comm.strip()).lstrip("-") == "claude":
            return f"w{pid}-{hashlib.md5(lstart.encode()).hexdigest()[:8]}"
        if not ppid.isdigit() or int(ppid) <= 1:
            break
        pid = int(ppid)
    return f"s-{sid}"


def records_dir(sd: Path) -> Path:
    return core.evolve_dir(sd) / "records"


def attach_record(sd: Path, st: dict, sess: dict, sid: str, tp: str, cwd, ts) -> str:
    """Return the record id this session writes to for this skill: the one the
    same window already uses (e.g. before a /clear), or a new one."""
    rd = core.run_dir()
    if not sess.get("window"):
        sess["window"] = window_key(sid)
    wfile = rd / "windows" / f"{sess['window']}.json"
    wstate = core.read_json(wfile) or {"records": {}}
    rdir = records_dir(sd)
    rdir.mkdir(parents=True, exist_ok=True)
    core.ensure_evolve_gitignore(sd)     # skills enabled by an older release lack it
    rid = wstate["records"].get(sd.name)
    if rid and (rdir / f"{rid}.md").is_file():
        meta = core.read_json(rdir / f"{rid}.json") or {}
        if sid not in meta.get("sessions", []):
            meta.setdefault("sessions", []).append(sid)
            core.write_json(rdir / f"{rid}.json", meta)
            with open(rdir / f"{rid}.md", "a", encoding="utf-8") as f:
                f.write(f"\n> [new session in the same window · {when(ts)} · session {sid} · transcript {tp}]\n")
        return rid
    rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{sid[:8]}"
    n = 1
    while (rdir / f"{rid}.md").exists():
        n += 1
        rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{sid[:8]}-{n}"
    (rdir / f"{rid}.md").write_text(HEADER.format(
        skill=sd.name, rid=rid, ver=st.get("version"), started=core.now_stamp(),
        cwd=cwd or "?", sid=sid, tp=tp, ep=sess.get("entrypoint") or "?", max=MAX_TEXT), encoding="utf-8")
    core.write_json(rdir / f"{rid}.json", {
        "record": rid, "skill": sd.name, "version": st.get("version"), "turns": 0,
        "sessions": [sid], "window": sess["window"], "project": cwd,
        "started": core.now_iso(), "updated": core.now_iso(), "tidied_turns": 0,
    })
    wstate["records"][sd.name] = rid
    core.write_json(wfile, wstate)
    return rid


def write_events(sd: Path, rid: str, st: dict, evs, results, who: str = "**User**") -> None:
    rdir = records_dir(sd)
    meta = core.read_json(rdir / f"{rid}.json") or {"record": rid, "turns": 0}
    head = ""
    if meta.get("version") != st.get("version"):
        if meta.get("version") is not None:
            head = f"\n> [skill version: v{meta.get('version')} → v{st.get('version')}]\n"
        meta["version"] = st.get("version")
    body, turn = render(evs, results, sd, int(meta.get("turns", 0)), who)
    if not (head or body):
        return
    with open(rdir / f"{rid}.md", "a", encoding="utf-8") as f:
        f.write(head + body)
    meta["turns"] = turn
    meta["updated"] = core.now_iso()
    core.write_json(rdir / f"{rid}.json", meta)


# ---------------------------------------------------------------- main flow

def enabled_names() -> dict:
    """Invocation name -> skill folder, for every enabled skill. The frontmatter name is
    read live: a renamed skill is invoked by its new name."""
    out = {}
    for sd in core.enabled_skill_dirs():
        st = core.load_state(sd) or {}
        for n in {sd.name, core.frontmatter_name(sd) or st.get("name") or sd.name}:
            out[n] = sd
    return out


def _time(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.astimezone()
    except Exception:
        return None


def _not_before(ts, since) -> bool:
    """False only when ts is known to be earlier than since."""
    a, b = _time(ts), _time(since)
    return a is None or b is None or a >= b


def _state_for(sd: Path):
    """State after storing an outside edit as a version. A failed capture is logged and
    does not stop recording."""
    try:
        return core.check_external(sd)
    except core.EvoError as e:
        log(f"ERROR version capture skipped for {sd.name}: {e}")
        return core.load_state(sd)


def prune_run_files() -> None:
    cutoff = time.time() - 30 * 86400
    for sub in ("sessions", "windows"):
        d = core.run_dir() / sub
        if not d.is_dir():
            continue
        for p in d.iterdir():
            with contextlib.suppress(OSError):
                if p.stat().st_mtime < cutoff:
                    p.unlink()


def process(payload: dict) -> None:
    sid = payload.get("session_id")
    tp = payload.get("transcript_path")
    if not sid or not tp or not os.path.isfile(tp):
        return
    enabled = enabled_names()
    sfile = core.run_dir() / "sessions" / f"{sid}.json"
    sess = core.read_json(sfile)
    if not enabled and not sess:
        return                                   # nothing is enabled: stay out of the way
    sfile.parent.mkdir(parents=True, exist_ok=True)
    lock = sfile.with_suffix(".lock")
    if not core.take_lock(lock, SESSION_LOCK_WAIT, 60):
        log(f"SKIP session {sid}: another recorder run holds {lock.name}; its new lines wait for the next run")
        return
    try:
        sess = core.read_json(sfile) or {"offset": 0, "active": {}, "window": None}
        active = sess.setdefault("active", {})
        # offset = how far invocations were looked for; offsets[skill] = how far that skill
        # was written. One skill failing then neither stops nor duplicates the others.
        offsets = sess.setdefault("offsets", {})
        scan = int(sess.get("offset", 0))
        for name in active:
            offsets.setdefault(name, scan)       # session files from before per-skill offsets
        size = os.path.getsize(tp)
        if size < scan or any(o > size for o in offsets.values()):
            scan = 0                             # the transcript was rewritten: start over
            for name in offsets:
                offsets[name] = 0
        base = min([scan, *offsets.values()])
        if size == base:
            return
        with open(tp, "rb") as f:
            f.seek(base)
            data = f.read()
        cut = data.rfind(b"\n")
        if cut < 0:
            return
        data = data[: cut + 1]
        if not sess.get("entrypoint"):
            m = ENTRYPOINT_RE.search(data)
            if m:
                sess["entrypoint"] = m.group(1).decode("utf-8", "replace")
        raw_lines = data.split(b"\n")
        queue = sess.setdefault("queue", [])
        queue_before = [list(q) for q in queue]
        events, results, ev_lines = parse(raw_lines, queue)
        if events and events[-1][0] == "slash":
            # What tells a built-in (/clear, /model) from a prompt is the stdout marker written
            # right after it. If that marker is not here yet, leave the command for the next run,
            # and re-parse what is kept so the input queue matches the kept lines exactly.
            hold = sum(len(ln) + 1 for ln in raw_lines[: ev_lines[-1]])
            data = data[:hold]
            queue[:] = queue_before
            if not data:
                return
            raw_lines = data.split(b"\n")
            events, results, ev_lines = parse(raw_lines, queue)
        end = base + len(data)
        line_pos, p = [], base
        for ln in raw_lines:
            line_pos.append(p)
            p += len(ln) + 1
        epos = [line_pos[n] for n in ev_lines]

        dirs = {sd.name: sd for sd in enabled.values()}
        starts = {}
        for name, off in offsets.items():        # active, or found earlier but not written yet
            if name in dirs:
                starts[name] = next((i for i, q in enumerate(epos) if q >= off), len(events))
        since = {}
        turn_start = 0
        for i, ev in enumerate(events):
            if ev[0] in ("prompt", "slash"):
                turn_start = i
            if epos[i] < scan:
                continue                         # already looked at by an earlier run
            hit = None
            if ev[0] == "slash":
                hit = enabled.get(ev[1])
            elif ev[0] == "tool" and ev[2] == "Skill":
                hit = enabled.get(str((ev[3] or {}).get("skill") or "").lstrip("/"))
            if hit is None or hit.name in starts:
                continue
            if hit.name not in since:
                since[hit.name] = (core.load_state(hit) or {}).get("enabled_at")
            if not _not_before(ev[-1], since[hit.name]):
                continue                         # used before evolution was enabled
            starts[hit.name] = turn_start        # record from the prompt that led to the skill
            offsets[hit.name] = epos[turn_start]

        who = "**Prompt (non-interactive)**" if (sess.get("entrypoint") or "").startswith("sdk") else "**User**"
        for name, start in starts.items():
            sd = dirs[name]
            try:
                st = _state_for(sd)
                if not st:
                    continue
                evs = events[start:]
                if name not in active:
                    first_ts = evs[0][-1] if evs else None
                    active[name] = attach_record(sd, st, sess, sid, tp, payload.get("cwd"), first_ts)
                write_events(sd, active[name], st, evs, results, who)
                offsets[name] = end
                core.write_json(sfile, sess)
            except Exception:
                log(f"ERROR recording {name}: " + traceback.format_exc())
        for name in offsets:
            if name not in dirs:
                offsets[name] = end              # disabled meanwhile: its turns are not wanted

        sess["offset"] = end
        core.write_json(sfile, sess)
        if int(time.time()) % 50 == 0:
            prune_run_files()
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def main() -> int:
    if os.environ.get(core.BG_ENV):
        return 0
    try:
        payload = json.load(sys.stdin)
        if payload.get("hook_event_name") in (None, "Stop", "SessionEnd"):
            process(payload)
    except Exception:
        log("ERROR " + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
