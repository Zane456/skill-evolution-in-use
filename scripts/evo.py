#!/usr/bin/env python3
"""Command line for skill-evolution-in-use.

  evo.py enable <skill>        start recording real use of a skill (stores v1)
  evo.py status [<skill>]      state of one skill, or of every enabled skill
  evo.py versions <skill>      stored versions and why each exists
  evo.py rollback <skill> vN   make the skill equal to version N; "prev" = undo the last change
  evo.py snapshot <skill>      store the current content as a new version now
  evo.py lock|unlock <skill>   forbid / allow automatic edits (recording continues)
  evo.py disable <skill>       stop recording; nothing is deleted
  evo.py install-hooks | uninstall-hooks | install-checkup

<skill> is a folder name under the skills root, a skill name, or a path.
Exit codes: 0 ok, 2 refused, failed or bad arguments, 3 skill-checkup missing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evo_core as core  # noqa: E402

TAG = core.TAG


def say(*parts) -> None:
    print(TAG, *parts)


def kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def hooks_line() -> str:
    have = core.hooks_installed()
    if len(have) == len(core.HOOK_EVENTS):
        return "installed (" + ", ".join(have) + ")"
    return "NOT installed — recording is off (run: evo.py install-hooks)"


def hook_errors_line():
    """The recorder never interrupts Claude Code, so its failures are silent.
    Surface them here, or a broken recorder just looks like 'no records yet'."""
    lp = core.run_dir() / "hook.log"
    try:
        errs = [ln for ln in lp.read_text(encoding="utf-8", errors="replace").splitlines() if " ERROR " in ln]
    except OSError:
        return None
    if not errs:
        return None
    return f"{len(errs)} recorder error(s), last at {errs[-1][:19].replace('T', ' ')} — details: {lp}"


def cmd_enable(a) -> int:
    sd = core.resolve_skill(a.skill)
    reason = core.refuse_reason(sd)
    if reason:
        say(f"refused: cannot enable '{sd.name}' because {reason}")
        return 2
    if not a.skip_checkup and not core.checkup_present():
        say("skill-checkup is not installed; later evolution steps depend on it.")
        say(f"tell the user, then run: evo.py install-checkup  (clones {core.CHECKUP_REPO})")
        say(f"then run enable again. Nothing was changed for '{sd.name}'.")
        return 3
    if not a.no_hooks:
        core._load_settings()            # a broken settings.json refuses before anything is enabled
    res = core.enable(sd)
    if res["already"]:
        say(f"already enabled: {sd.name} · v{res['version']}")
    elif res["reenabled"]:
        say(f"enabled again: {sd.name} · v{res['version']} (earlier records and versions kept)")
    else:
        say(f"enabled: {sd.name} · v{res['version']} · {res['files']} files stored")
    say(f"data folder: {core.evolve_dir(sd)}/")
    later = "recording starts the next time the skill is invoked, also in sessions already open"
    if a.no_hooks:
        say("hooks: skipped (--no-hooks); " + (later if len(core.hooks_installed()) == len(core.HOOK_EVENTS)
                                               else "nothing is recorded until they are installed"))
    else:
        h = core.install_hooks()
        if h["added"] or h["repaired"]:
            say(f"hooks: installed in {core.settings_path()} ({', '.join(h['added'] + h['repaired'])}); "
                "recording starts in the NEXT Claude Code session (hooks load at session start)")
            if h["backup"]:
                say(f"settings backup: {h['backup']}")
        else:
            say("hooks: already installed; " + later)
    say("the skill's own files were not modified")
    return 0


def print_status(sd: Path) -> None:
    s = core.status(sd)
    st = s["state"]
    print(f"{TAG} {s['skill']}")
    print(f"  state      {'enabled' if st.get('enabled') else 'DISABLED'} · "
          f"{'LOCKED' if st.get('locked') else 'unlocked'} · v{st.get('version')}")
    print("  live copy  " + (f"differs from v{st.get('version')} (stored as a new version the next time the skill is used)"
                             if s["dirty"] else f"matches v{st.get('version')}"))
    rec = f"{s['records']} file(s) · {kb(s['records_bytes'])}"
    if s["latest_record"]:
        rec += f" · latest {s['latest_record']}"
    print(f"  records    {rec}")
    print("  versions   " + " · ".join(f"v{v['version']} ({v['actor']})" for v in s["versions"]))
    for v in s["versions"][-3:]:
        print(f"  recent     v{v['version']} · {str(v['created_at'])[:16].replace('T', ' ')} · {v['actor']} — {v['note']}")
    print(f"  folder     {s['path']}/evolve/ · {kb(s['evolve_bytes'])}")
    print(f"  hooks      {hooks_line()}")
    err = hook_errors_line()
    if err:
        print(f"  WARNING    {err}")


def cmd_status(a) -> int:
    if a.skill:
        print_status(core.resolve_skill(a.skill))
        return 0
    dirs = core.enabled_skill_dirs()
    if not dirs:
        say("no skill has evolution enabled yet (run: evo.py enable <skill>)")
        say(f"hooks: {hooks_line()}")
        return 0
    for sd in dirs:
        print_status(sd)
    return 0


def cmd_versions(a) -> int:
    sd = core.resolve_skill(a.skill)
    st = core.require_state(sd)
    print(f"{TAG} {sd.name} · current v{st.get('version')}")
    for m in core.list_versions(sd):
        print(f"  v{m['version']} · {str(m['created_at'])[:16].replace('T', ' ')} · {m['actor']} · "
              f"{len(m['files'])} files — {m['note']}")
    print(f"  details per version: {core.evolve_dir(sd)}/changelog.md")
    return 0


def cmd_rollback(a) -> int:
    sd = core.resolve_skill(a.skill)
    raw = str(a.version).lower().lstrip("v")
    if raw != "prev" and not raw.isdigit():
        say(f"refused: '{a.version}' is not a version (write v2, 2 or prev)")
        return 2
    res = core.rollback(sd, raw if raw == "prev" else int(raw))
    if res["captured"]:
        say(f"unsaved edits were stored first as v{res['captured']}")
    if res["noop"]:
        say(f"nothing to do: {sd.name} already equals v{res['restored']} (current v{res['version']})")
        return 0
    for w in res["warnings"]:
        say("warning:", w)
    if res["mismatch"]:
        say(f"failed: {sd.name} does not equal v{res['restored']} after the rollback (stored as v{res['version']}); "
            f"differing: {', '.join(res['mismatch'][:10])}. The state before it is v{res['version'] - 1}.")
        return 2
    say(f"rolled back: {sd.name} now has the content of v{res['restored']}, stored as v{res['version']}")
    say(f"to undo this rollback: evo.py rollback {sd.name} v{res['version'] - 1}")
    return 0


def cmd_snapshot(a) -> int:
    sd = core.resolve_skill(a.skill)
    ver, created = core.snapshot(sd, "manual", a.note or "stored on request")
    say(f"stored: {sd.name} · v{ver}" if created else f"no change since v{ver}; nothing stored")
    return 0


def cmd_flag(key: str, value: bool, done: str, same: str):
    def run(a) -> int:
        sd = core.resolve_skill(a.skill)
        res = core.set_flag(sd, key, value)
        say((done if res["changed"] else same).format(skill=sd.name))
        return 0
    return run


def cmd_install_hooks(_a) -> int:
    h = core.install_hooks()
    if h["added"] or h["repaired"]:
        say(f"hooks installed in {core.settings_path()} ({', '.join(h['added'] + h['repaired'])}); "
            "they take effect in the NEXT Claude Code session")
        if h["backup"]:
            say(f"settings backup: {h['backup']}")
    else:
        say("hooks already installed")
    return 0


def cmd_uninstall_hooks(_a) -> int:
    h = core.uninstall_hooks()
    if h["removed"]:
        say(f"hooks removed from {core.settings_path()} ({', '.join(h['removed'])}); recording stops in new sessions")
        if h["backup"]:
            say(f"settings backup: {h['backup']}")
    else:
        say("no hooks of this skill were installed")
    return 0


def cmd_install_checkup(_a) -> int:
    r = core.install_checkup()
    say(f"skill-checkup: {'already present' if r == 'present' else r} at {core.checkup_dir()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="evo.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("enable")
    p.add_argument("skill")
    p.add_argument("--no-hooks", action="store_true", help="do not touch settings.json")
    p.add_argument("--skip-checkup", action="store_true", help="do not require skill-checkup")
    p.set_defaults(fn=cmd_enable)

    p = sub.add_parser("status")
    p.add_argument("skill", nargs="?")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("versions")
    p.add_argument("skill")
    p.set_defaults(fn=cmd_versions)

    p = sub.add_parser("rollback")
    p.add_argument("skill")
    p.add_argument("version")
    p.set_defaults(fn=cmd_rollback)

    p = sub.add_parser("snapshot")
    p.add_argument("skill")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_snapshot)

    for name, key, val, done, same in (
        ("lock", "locked", True, "locked: {skill} — recording continues, automatic edits are off",
         "{skill} was already locked"),
        ("unlock", "locked", False, "unlocked: {skill}", "{skill} was not locked"),
        ("disable", "enabled", False, "disabled: {skill} — recording stops; records and versions are kept",
         "{skill} was already disabled"),
    ):
        p = sub.add_parser(name)
        p.add_argument("skill")
        p.set_defaults(fn=cmd_flag(key, val, done, same))

    sub.add_parser("install-hooks").set_defaults(fn=cmd_install_hooks)
    sub.add_parser("uninstall-hooks").set_defaults(fn=cmd_uninstall_hooks)
    sub.add_parser("install-checkup").set_defaults(fn=cmd_install_checkup)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except core.EvoError as e:
        say("refused:", e)
        return 2
    except OSError as e:
        say(f"failed: {e.strerror or e}" + (f" ({e.filename})" if e.filename else ""))
        return 2


if __name__ == "__main__":
    sys.exit(main())
