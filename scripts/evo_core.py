#!/usr/bin/env python3
"""Core library for skill-evolution-in-use (stdlib only).

Everything a target skill accumulates lives in <skill>/evolve/. Nothing in
this file calls a model, and nothing here runs code from the target skill.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

EVOLVE = "evolve"
BG_ENV = "SKILL_EVOLUTION_IN_USE_BG"      # set by background runs so hooks skip them
SNAP_SUFFIX = ".snapshot"                 # SKILL.md is stored as SKILL.md.snapshot
LARGE_FILE = 2 * 1024 * 1024              # larger files are tracked by size, not copied
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
SKIP_FILES = {".DS_Store"}
HOOK_EVENTS = ("Stop", "SessionEnd")
HOOK_SCRIPT = "record_hook.py"
CHECKUP_REPO = "https://github.com/Zane456/skill-checkup"
TAG = "[skill-evolution-in-use]"
HOOK_MARK = "# skill-evolution-in-use"   # ends our hook command; finds it whatever the folder is called


class EvoError(Exception):
    """A problem the user can act on; the message is shown as-is."""


# ---------------------------------------------------------------- paths

def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def skills_root() -> Path:
    return Path(os.environ.get("SKILL_EVOLUTION_SKILLS_ROOT") or config_dir() / "skills")


def run_dir() -> Path:
    return Path(os.environ.get("SKILL_EVOLUTION_RUN_DIR")
                or config_dir() / "skill-evolution-in-use" / "run")


def evolve_dir(sd: Path) -> Path:
    return sd / EVOLVE


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- json io

def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p, obj) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- locating a skill

def frontmatter_name(sd: Path):
    try:
        head = (sd / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    m = re.match(r"\s*---\s*\n(.*?)\n---", head, re.S)
    if not m:
        return None
    n = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", m.group(1))
    return n.group(1).strip() if n else None


def _is_skill(p: Path) -> bool:
    return p.is_dir() and (p / "SKILL.md").is_file()


def resolve_skill(arg: str) -> Path:
    """Accept a folder name under the skills root, a frontmatter name, or a path."""
    arg = (arg or "").strip()
    if not arg:
        raise EvoError("no skill given")
    looks_like_path = arg.startswith(("~", ".", "/")) or os.sep in arg
    if looks_like_path:
        p = Path(arg).expanduser()
        if _is_skill(p):
            return p.resolve()
        raise EvoError(f"'{arg}' is not a skill folder (no SKILL.md inside)")
    root = skills_root()
    if _is_skill(root / arg):
        return (root / arg).resolve()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if _is_skill(d) and frontmatter_name(d) == arg:
                return d.resolve()
    raise EvoError(f"no skill named '{arg}' under {root}")


def refuse_reason(sd: Path):
    """Why this skill must not be enabled, or None."""
    parts = sd.parts
    for i, seg in enumerate(parts[:-1]):
        if seg == "skills" and parts[i + 1] == "synced":
            return "it is an account-synced skill (skills/synced/): the sync overwrites local changes"
    if "plugins" in parts[:-1]:
        return "it lives in a plugin folder: plugin updates overwrite local changes"
    if not os.access(sd, os.W_OK):
        return "its folder is not writable"
    return None


# ---------------------------------------------------------------- content model

def iter_files(sd: Path):
    for dp, dns, fns in os.walk(sd):
        top = Path(dp) == sd
        dns[:] = sorted(d for d in dns
                        if d not in SKIP_DIRS and not (top and d == EVOLVE))
        for fn in sorted(fns):
            if fn in SKIP_FILES or fn.endswith(".pyc"):
                continue
            p = Path(dp) / fn
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(sd: Path) -> dict:
    files = {}
    for p in iter_files(sd):
        rel = p.relative_to(sd).as_posix()
        size = p.stat().st_size
        if size > LARGE_FILE:
            files[rel] = {"size": size, "large": True}
        else:
            files[rel] = {"size": size, "sha": sha256_file(p)}
    return files


def tree_hash(files: dict) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        f = files[rel]
        h.update(f"{rel}\t{f.get('sha') or 'large:' + str(f['size'])}\n".encode())
    return h.hexdigest()


def fingerprint(sd: Path) -> str:
    """Cheap change detector (names, sizes, mtimes); a full hash only runs when it moves."""
    h = hashlib.sha256()
    for p in iter_files(sd):
        s = p.stat()
        h.update(f"{p.relative_to(sd).as_posix()}\t{s.st_size}\t{s.st_mtime_ns}\n".encode())
    return h.hexdigest()


def snap_name(rel: str) -> str:
    """Stored name of a file inside versions/vN/. A stored SKILL.md must never be
    discoverable as a skill, so it gets a suffix."""
    return rel + SNAP_SUFFIX if rel.rsplit("/", 1)[-1] == "SKILL.md" else rel


def diff_manifests(old: dict, new: dict) -> dict:
    def key(f):
        return f.get("sha") or f"large:{f['size']}"
    return {
        "added": sorted(r for r in new if r not in old),
        "modified": sorted(r for r in new if r in old and key(new[r]) != key(old[r])),
        "removed": sorted(r for r in old if r not in new),
    }


# ---------------------------------------------------------------- state + lock

def state_path(sd: Path) -> Path:
    return evolve_dir(sd) / "state.json"


def load_state(sd: Path):
    return read_json(state_path(sd))


def save_state(sd: Path, st: dict) -> None:
    write_json(state_path(sd), st)


def require_state(sd: Path) -> dict:
    st = load_state(sd)
    if not st:
        raise EvoError(f"evolution was never enabled for '{sd.name}' (run: enable {sd.name})")
    return st


def _lock_is_stale(lp: Path, max_age: float) -> bool:
    """A lock is stale when the PID written in it is dead, or when it is older than max_age."""
    try:
        pid = lp.read_text(encoding="utf-8").strip()
        age = time.time() - lp.stat().st_mtime
    except OSError:
        return False                     # gone already: the next attempt takes it
    if pid.isdigit() and os.name != "nt":   # on Windows os.kill(pid, 0) terminates the process
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass                         # alive, owned by someone else
    return age > max_age


def take_lock(lp: Path, wait: float, max_age: float) -> bool:
    """Create lock file `lp` holding our PID. False if a live holder keeps it past `wait` seconds."""
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lp, max_age):
                with contextlib.suppress(OSError):
                    lp.unlink()
                continue
            if time.time() > deadline:
                return False
            time.sleep(0.05)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True


@contextmanager
def locked(sd: Path, wait: float = 5.0):
    lp = evolve_dir(sd) / ".lock"
    lp.parent.mkdir(parents=True, exist_ok=True)
    if not take_lock(lp, wait, 600):
        raise EvoError(f"another operation is working on '{sd.name}'; try again shortly")
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lp.unlink()


# ---------------------------------------------------------------- versions

def version_dir(sd: Path, n: int) -> Path:
    return evolve_dir(sd) / "versions" / f"v{n}"


def version_manifest(sd: Path, n: int):
    return read_json(version_dir(sd, n) / "manifest.json")


def list_versions(sd: Path) -> list:
    out = []
    vroot = evolve_dir(sd) / "versions"
    if vroot.is_dir():
        for d in vroot.iterdir():
            m = re.fullmatch(r"v(\d+)", d.name)
            man = read_json(d / "manifest.json") if m else None
            if man:
                out.append(man)
    return sorted(out, key=lambda m: m["version"])


def _append_changelog(sd: Path, n: int, actor: str, note: str, changes: dict, total: int) -> None:
    cl = evolve_dir(sd) / "changelog.md"
    lines = []
    if not cl.exists():
        lines.append(f"# Changelog · {sd.name}\n\n"
                     "One entry per stored version. `actor` says who changed the skill: "
                     "enable, external (edited outside this tool), manual (stored on request), rollback, "
                     "background, user-approved.\n")
    lines.append(f"\n## v{n} · {now_stamp()} · {actor}\n")
    if note:
        lines.append(f"\n{note}\n")
    bullets = [f"- {k}: {', '.join(v)}" for k, v in changes.items() if v]
    if not bullets:
        bullets = [f"- files stored: {total}"]
    lines.append("\n" + "\n".join(bullets) + "\n")
    with open(cl, "a", encoding="utf-8") as f:
        f.write("".join(lines))


def _move_aside(p: Path) -> None:
    """Keep debris of an interrupted run out of the way; nothing is deleted."""
    os.replace(p, p.with_name(f".{p.name}.debris-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"))


def _snapshot(sd: Path, st: dict, actor: str, note: str, force: bool = False):
    """Store the live content as the next version. Caller holds the lock.
    Returns (version, created)."""
    files = build_manifest(sd)
    th = tree_hash(files)
    prev = int(st.get("version", 0))
    if prev >= 1 and th == st.get("tree_hash") and not force:
        st["fingerprint"] = fingerprint(sd)
        save_state(sd, st)
        return prev, False
    # a run killed after storing vN but before saving state.json leaves vN behind: never reuse it
    new = max([prev] + [m["version"] for m in list_versions(sd)]) + 1
    vdir = version_dir(sd, new)
    if vdir.exists():                    # no manifest.json, or list_versions would have counted it
        _move_aside(vdir)
    tmp = vdir.with_name(f".{vdir.name}.tmp-{os.getpid()}")
    if tmp.exists():
        _move_aside(tmp)
    for rel, meta in files.items():
        if meta.get("large"):
            continue
        dst = tmp / snap_name(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sd / rel, dst)
    write_json(tmp / "manifest.json", {
        "version": new, "created_at": now_iso(), "actor": actor, "note": note,
        "tree_hash": th, "files": files,
    })
    os.replace(tmp, vdir)                # a version appears only when complete
    prev_files = (version_manifest(sd, prev) or {}).get("files", {}) if prev else {}
    changes = diff_manifests(prev_files, files) if prev else {}
    _append_changelog(sd, new, actor, note, changes, len(files))
    st.update(version=new, tree_hash=th, fingerprint=fingerprint(sd), updated_at=now_iso())
    save_state(sd, st)
    return new, True


def snapshot(sd: Path, actor: str, note: str, force: bool = False):
    with locked(sd):
        return _snapshot(sd, require_state(sd), actor, note, force)


def check_external(sd: Path):
    """Called at use time: if the skill was edited outside this tool, store that
    as a new version so every record points at content that really existed."""
    st = load_state(sd)
    if not st or not st.get("enabled"):
        return st
    if fingerprint(sd) == st.get("fingerprint"):
        return st
    with locked(sd, wait=0.5):           # EvoError goes to the caller, which logs it
        st = require_state(sd)
        _snapshot(sd, st, "external", "edited outside skill-evolution-in-use (noticed when the skill was used)")
    return load_state(sd)


# ---------------------------------------------------------------- enable / flags

def ensure_evolve_gitignore(sd: Path) -> None:
    """Records are real conversations: keep them out of any git repo around the skill
    (the skill's own, or a parent such as a versioned ~/.claude), without editing the skill."""
    gi = evolve_dir(sd) / ".gitignore"
    if not gi.is_file() or gi.read_text(encoding="utf-8") != "*\n":
        gi.write_text("*\n", encoding="utf-8")


def registry_path() -> Path:
    return run_dir() / "enabled.json"


def _register(sd: Path, on: bool) -> None:
    """enabled.json lists enabled skill folders, so the recorder also finds those outside the skills root."""
    reg = read_json(registry_path()) or {}
    have = [p for p in reg.get("skills", []) if p != str(sd)]
    write_json(registry_path(), {"skills": have + ([str(sd)] if on else [])})


def enable(sd: Path) -> dict:
    reason = refuse_reason(sd)
    if reason:
        raise EvoError(f"cannot enable '{sd.name}': {reason}")
    ev = evolve_dir(sd)
    (ev / "records").mkdir(parents=True, exist_ok=True)
    (ev / "versions").mkdir(exist_ok=True)
    ensure_evolve_gitignore(sd)
    with locked(sd):
        st = load_state(sd)
        if st:
            was = bool(st.get("enabled"))
            st["enabled"] = True
            if not was:
                st["enabled_at"] = now_iso()     # use while it was disabled is not recorded
            save_state(sd, st)
            ver, _ = _snapshot(sd, st, "external", "change found when evolution was enabled again")
            res = {"already": was, "reenabled": not was, "version": ver,
                   "files": len((version_manifest(sd, ver) or {}).get("files", {}))}
        else:
            st = {"schema": 1, "skill": sd.name, "name": frontmatter_name(sd) or sd.name,
                  "enabled": True, "locked": False, "version": 0, "enabled_at": now_iso()}
            ver, _ = _snapshot(sd, st, "enable", "content at the moment evolution was enabled")
            res = {"already": False, "reenabled": False, "version": ver,
                   "files": len((version_manifest(sd, ver) or {}).get("files", {}))}
    _register(sd, True)
    return res


def set_flag(sd: Path, key: str, value: bool) -> dict:
    with locked(sd):
        st = require_state(sd)
        changed = bool(st.get(key)) != value
        st[key] = value
        st["updated_at"] = now_iso()
        save_state(sd, st)
    if key == "enabled":
        _register(sd, value)
    return {"changed": changed, "state": st}


# ---------------------------------------------------------------- rollback

def _prune_empty_dirs(sd: Path) -> None:
    for dp, dns, fns in os.walk(sd, topdown=False):
        p = Path(dp)
        if p == sd:
            continue
        rel = p.relative_to(sd).parts
        if rel[0] == EVOLVE or any(seg in SKIP_DIRS for seg in rel):
            continue
        with contextlib.suppress(OSError):
            if not any(p.iterdir()):
                p.rmdir()


def _holds_large(p: Path) -> bool:
    return any(f.stat().st_size > LARGE_FILE for f in iter_files(p))


def _clear_for_file(sd: Path, rel: str, kept: set, warnings: list, warned: set) -> bool:
    """Make room for the stored file `rel`: a symlink or a folder standing at its path, or a
    file standing where one of its parent folders must go. A symlink is removed, never written
    through. False (with a warning) when the obstacle is a large file, which is never removed
    because it has no stored copy."""
    dst = sd / rel
    parts = rel.split("/")
    for i in range(1, len(parts)):
        p, prel = sd.joinpath(*parts[:i]), "/".join(parts[:i])
        if p.is_symlink() or (p.exists() and not p.is_dir()):
            if prel in kept:
                warnings.append(f"could not restore {rel}: the large file {prel} is in the way and is kept")
                return False
            p.unlink()
            if prel not in warned:
                warnings.append(f"removed {prel}: the stored version has a folder there")
                warned.add(prel)
    if dst.is_symlink():
        dst.unlink()
        warnings.append(f"replaced the symlink {rel} with the stored file")
    elif dst.is_dir():
        if _holds_large(dst):
            warnings.append(f"could not restore {rel}: the folder at that path holds a large file and is kept")
            return False
        shutil.rmtree(dst)
    return True


def _mismatch(live: dict, want: dict) -> list:
    """Paths where the live tree differs from the wanted one; large entries are left aside."""
    aside = {r for f in (live, want) for r, m in f.items() if m.get("large")}
    a = {r: m["sha"] for r, m in live.items() if r not in aside}
    b = {r: m["sha"] for r, m in want.items() if r not in aside}
    return sorted(r for r in set(a) | set(b) if a.get(r) != b.get(r))


def rollback(sd: Path, target) -> dict:
    """Make the live skill equal to version `target` (a number, or "prev"). The state
    before the rollback is stored first, and the result becomes a NEW version, so a
    rollback can itself be rolled back. Files over LARGE_FILE are never overwritten or
    deleted: they have no stored copy."""
    with locked(sd):
        st = require_state(sd)
        cur = build_manifest(sd)
        dirty = tree_hash(cur) != st.get("tree_hash")
        if target == "prev":             # unsaved edits: back to the stored version; else one version back
            target = int(st.get("version", 0)) - (0 if dirty else 1)
            if target < 1:
                raise EvoError(f"'{sd.name}' has no version before v{st.get('version')}")
        tman = version_manifest(sd, target)
        if not tman:
            have = ", ".join(f"v{m['version']}" for m in list_versions(sd)) or "none"
            raise EvoError(f"v{target} does not exist for '{sd.name}' (stored: {have})")
        tfiles = tman["files"]
        tdir = version_dir(sd, target)
        missing = [r for r, m in tfiles.items() if not m.get("large") and not (tdir / snap_name(r)).is_file()]
        if missing:                      # checked before anything is changed
            raise EvoError(f"v{target} of '{sd.name}' is incomplete (stored copy missing: {', '.join(missing[:5])}); "
                           "nothing was changed")
        captured = None
        if dirty:
            captured, _ = _snapshot(sd, st, "external", "unsaved edits stored before a rollback")
            cur = build_manifest(sd)
        if tree_hash(cur) == tman["tree_hash"]:
            return {"noop": True, "version": st["version"], "restored": target,
                    "captured": captured, "warnings": [], "mismatch": []}
        warnings, warned = [], set()
        kept = {r for r, m in cur.items() if m.get("large")}
        for rel, meta in cur.items():
            if rel in tfiles:
                continue
            if meta.get("large"):
                warnings.append(f"kept {rel}: large file that v{target} does not have")
                continue
            (sd / rel).unlink()
        for rel, meta in tfiles.items():
            if meta.get("large"):
                live = sd / rel
                if not live.is_file() or live.stat().st_size != meta["size"]:
                    warnings.append(f"{rel} differs from v{target} but large files are not stored")
                continue
            if rel in kept:
                warnings.append(f"kept {rel}: it grew past {LARGE_FILE // 1024 // 1024} MB, so it has no stored "
                                f"copy and was not replaced by the one in v{target}")
                continue
            if not _clear_for_file(sd, rel, kept, warnings, warned):
                continue
            dst = sd / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tdir / snap_name(rel), dst)
        _prune_empty_dirs(sd)
        mismatch = _mismatch(build_manifest(sd), tfiles)
        note = f"restored the content of v{target}" + (f" (incomplete: {len(mismatch)} path(s) differ)" if mismatch else "")
        new, _ = _snapshot(sd, st, "rollback", note, force=True)
        return {"noop": False, "version": new, "restored": target,
                "captured": captured, "warnings": warnings, "mismatch": mismatch}


# ---------------------------------------------------------------- status

def tree_size(p: Path) -> int:
    total = 0
    for dp, _dns, fns in os.walk(p):
        for fn in fns:
            with contextlib.suppress(OSError):
                total += (Path(dp) / fn).stat().st_size
    return total


def status(sd: Path) -> dict:
    st = require_state(sd)
    ev = evolve_dir(sd)
    recs = sorted((ev / "records").glob("*.md"))
    dirty = False
    if fingerprint(sd) != st.get("fingerprint"):
        dirty = tree_hash(build_manifest(sd)) != st.get("tree_hash")
    return {
        "skill": sd.name, "path": str(sd), "state": st, "dirty": dirty,
        "records": len(recs),
        "records_bytes": sum(r.stat().st_size for r in recs),
        "latest_record": datetime.fromtimestamp(max(r.stat().st_mtime for r in recs)).strftime("%Y-%m-%d %H:%M") if recs else None,
        "versions": [{"version": m["version"], "actor": m.get("actor"), "note": m.get("note"),
                      "created_at": m.get("created_at")} for m in list_versions(sd)],
        "evolve_bytes": tree_size(ev),
    }


def enabled_skill_dirs() -> list:
    found = []
    root = skills_root()
    if root.is_dir():
        found += [sp.parent.parent for sp in sorted(root.glob(f"*/{EVOLVE}/state.json"))]
    found += [Path(p) for p in (read_json(registry_path()) or {}).get("skills", [])]
    out, seen = [], set()
    for sd in found:
        key = os.path.realpath(sd)
        st = read_json(state_path(sd))
        if key not in seen and st and st.get("enabled"):
            seen.add(key)
            out.append(sd)
    return out


# ---------------------------------------------------------------- hooks in settings.json

def settings_path() -> Path:
    return config_dir() / "settings.json"


def hook_command() -> str:
    return f'python3 "{Path(__file__).resolve().parent / HOOK_SCRIPT}" {HOOK_MARK}'


def _is_ours(cmd) -> bool:
    if not isinstance(cmd, str):
        return False
    return cmd.rstrip().endswith(HOOK_MARK) or (HOOK_SCRIPT in cmd and "skill-evolution-in-use" in cmd)


def _load_settings():
    sp = settings_path()
    if not sp.exists():
        return {}, 2
    raw = sp.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise EvoError(f"{sp} is not valid JSON ({e}); fix it before installing hooks")
    if not isinstance(data, dict):
        raise EvoError(f"{sp} does not hold a JSON object")
    m = re.search(r'\n([ \t]+)"', raw)
    indent = m.group(1) if m else "  "
    return data, ("\t" if "\t" in indent else len(indent))


def _write_settings(data: dict, indent) -> Path:
    sp = settings_path().resolve()       # a symlinked settings.json stays a symlink
    backup = None
    if sp.exists():
        backup = sp.with_name(f"{sp.name}.bak-skill-evolution-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(sp, backup)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_name(f"{sp.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    os.replace(tmp, sp)
    return backup


def hooks_installed() -> list:
    """Events whose hook list already runs our recorder at its current path."""
    try:
        data, _ = _load_settings()
    except EvoError:
        return []
    cmd = hook_command()
    found = []
    for ev in HOOK_EVENTS:
        for group in (data.get("hooks") or {}).get(ev) or []:
            if any(h.get("command") == cmd for h in (group.get("hooks") or []) if isinstance(h, dict)):
                found.append(ev)
                break
    return found


def install_hooks() -> dict:
    data, indent = _load_settings()
    cmd = hook_command()
    hooks = data.setdefault("hooks", {})
    added, repaired = [], []
    for ev in HOOK_EVENTS:
        groups = hooks.setdefault(ev, [])
        ours = [h for g in groups for h in (g.get("hooks") or [])
                if isinstance(h, dict) and _is_ours(h.get("command"))]
        if not ours:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
            added.append(ev)
        else:
            for h in ours:
                if h.get("command") != cmd:      # the skill folder moved
                    h["command"] = cmd
                    repaired.append(ev)
    backup = _write_settings(data, indent) if (added or repaired) else None
    return {"added": added, "repaired": repaired, "backup": str(backup) if backup else None}


def uninstall_hooks() -> dict:
    data, indent = _load_settings()
    hooks = data.get("hooks") or {}
    removed = []
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            hs = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hs, list):
                kept_groups.append(g)
                continue
            kept = [h for h in hs if not (isinstance(h, dict) and _is_ours(h.get("command")))]
            if len(kept) != len(hs):
                removed.append(ev)
            if kept:
                g["hooks"] = kept
                kept_groups.append(g)
        if kept_groups:
            hooks[ev] = kept_groups
        else:
            del hooks[ev]
    backup = _write_settings(data, indent) if removed else None
    return {"removed": sorted(set(removed)), "backup": str(backup) if backup else None}


# ---------------------------------------------------------------- skill-checkup dependency

def checkup_dir() -> Path:
    return skills_root() / "skill-checkup"


def checkup_present() -> bool:
    return (checkup_dir() / "SKILL.md").is_file()


def _fetch_checkup_git(tmp: Path) -> None:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")     # never wait on a credential prompt
    proc = subprocess.Popen(["git", "clone", "--depth", "1", CHECKUP_REPO, str(tmp)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            env=env, start_new_session=True)
    try:
        _, err = proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        # git's network helper is a child process; kill the whole group or it lingers
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, 15)
        proc.wait()
        raise EvoError("git clone gave up after 90 s (slow or blocked connection)")
    if proc.returncode != 0:
        raise EvoError((err or "git clone failed").strip())


def _fetch_checkup_tarball(tmp: Path) -> None:
    """Plain HTTPS download; works where `git clone` stalls (proxies, missing git)."""
    import io
    import tarfile
    import urllib.request
    url = CHECKUP_REPO.replace("https://github.com/", "https://codeload.github.com/") + "/tar.gz/refs/heads/main"
    data = urllib.request.urlopen(url, timeout=60).read()
    unpack = tmp.with_name(tmp.name + "-unpack")
    unpack.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            root = unpack.resolve()
            for m in tf.getmembers():                    # refuse anything escaping the folder
                if not str((unpack / m.name).resolve()).startswith(str(root)) or m.issym() or m.islnk():
                    raise EvoError(f"unsafe entry in downloaded archive: {m.name}")
            tf.extractall(unpack)
        tops = [d for d in unpack.iterdir() if d.is_dir()]
        if len(tops) != 1:
            raise EvoError("downloaded archive has an unexpected layout")
        os.replace(tops[0], tmp)
    finally:
        shutil.rmtree(unpack, ignore_errors=True)


def install_checkup() -> str:
    """Returns 'present', 'installed (download)' or 'installed (git)'."""
    if checkup_present():
        return "present"
    dst = checkup_dir()
    if dst.exists():
        if dst.is_dir() and not any(dst.iterdir()):
            dst.rmdir()                                  # empty leftover of an interrupted install
        else:
            raise EvoError(f"{dst} exists but holds no SKILL.md; inspect it before installing")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.installing-{os.getpid()}")
    errors = []
    # download first: one HTTPS request, needs no git, and survives slow links that time out a clone
    for how, fetch in (("download", _fetch_checkup_tarball), ("git", _fetch_checkup_git)):
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            fetch(tmp)
            if not (tmp / "SKILL.md").is_file():
                raise EvoError("no SKILL.md in what was fetched")
            os.replace(tmp, dst)                         # appears only when complete
            return f"installed ({how})"
        except Exception as e:                           # try the next way, keep the reason
            detail = getattr(e, "stderr", "") or str(e) or type(e).__name__
            errors.append(f"{how}: {' '.join(str(detail).split())[:160]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    raise EvoError(f"could not fetch {CHECKUP_REPO} ({'; '.join(errors)})")
