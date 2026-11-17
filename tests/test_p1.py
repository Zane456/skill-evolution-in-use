#!/usr/bin/env python3
"""P1 tests for skill-evolution-in-use. Everything runs in temp dirs through
env overrides; the real ~/.claude is never touched.

    python3 tests/test_p1.py      # tests the skill in this repo
    SEIU_SKILL_DIR=/path python3 tests/test_p1.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

SKILL = Path(os.environ.get("SEIU_SKILL_DIR") or Path(__file__).resolve().parent.parent)
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))
import evo_core as core      # noqa: E402
import record_hook as hook   # noqa: E402
import evo                   # noqa: E402

TS = "2099-11-15T12:00:00.000Z"       # later than any enabled_at a test creates
OLD_TS = "2020-01-01T00:00:00.000Z"   # earlier than it


def dump(entries, path, compact=True, mode="a"):
    with open(path, mode, encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":") if compact else None) + "\n")


def u_prompt(text):
    return {"type": "user", "isSidechain": False, "timestamp": TS, "message": {"role": "user", "content": text}}


def u_meta(text):
    return {"type": "user", "isMeta": True, "timestamp": TS,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def a_text(text):
    return {"type": "assistant", "isSidechain": False, "timestamp": TS,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def a_tool(tid, name, inp):
    return {"type": "assistant", "isSidechain": False, "timestamp": TS,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def u_result(tid, content, is_error=False):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        b["is_error"] = True
    return {"type": "user", "isSidechain": False, "timestamp": TS, "message": {"role": "user", "content": [b]}}


def blob(role, text):
    return {"type": "api-request-blob", "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def queued(prompt, kind="human", sidechain=False):
    """A message sent while Claude was working (attachment queued_command)."""
    return {"type": "attachment", "isSidechain": sidechain, "timestamp": TS, "entrypoint": "cli",
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
                           "origin": {"kind": kind}, "humanTurn": kind == "human", "timestamp": TS},
            "rendered": [{"content": "<system-reminder>\nThe user sent a new message while you were working:\n...\n</system-reminder>"}],
            "renderedRole": "system"}


def slash(name, args=""):
    return u_prompt(f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>\n<command-args>{args}</command-args>")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seiu-test-")).resolve()
        self.cfg = self.tmp / "claude"
        self.skills = self.cfg / "skills"
        self.skills.mkdir(parents=True)
        self._env = dict(os.environ)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        os.environ["SKILL_EVOLUTION_WINDOW_KEY"] = "win-A"
        for k in ("SKILL_EVOLUTION_SKILLS_ROOT", "SKILL_EVOLUTION_RUN_DIR", core.BG_ENV):
            os.environ.pop(k, None)
        self.sd = self.make_skill("demo")
        self.tp = self.tmp / "sess-1.jsonl"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_skill(self, dirname, name=None, parent=None):
        sd = (parent or self.skills) / dirname
        (sd / "references").mkdir(parents=True)
        (sd / "scripts").mkdir()
        (sd / "SKILL.md").write_text(f"---\nname: {name or dirname}\ndescription: demo skill\n---\n\n# Demo\n\nRun `scripts/run.py`.\n", encoding="utf-8")
        (sd / "references" / "guide.md").write_text("guide v1\n", encoding="utf-8")
        (sd / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")
        return sd.resolve()

    def run_hook(self, sid="sess-1", tp=None, cwd="/proj"):
        hook.process({"session_id": sid, "transcript_path": str(tp or self.tp), "cwd": cwd, "hook_event_name": "Stop"})

    def records(self, sd=None):
        return sorted(((sd or self.sd) / "evolve" / "records").glob("*.md"))

    def record_text(self, sd=None):
        recs = self.records(sd)
        self.assertEqual(len(recs), 1, f"expected exactly one record, got {recs}")
        return recs[0].read_text(encoding="utf-8")

    def log_text(self):
        lp = self.cfg / "skill-evolution-in-use" / "run" / "hook.log"
        return lp.read_text(encoding="utf-8") if lp.exists() else ""

    def tree(self, sd=None):
        return {r: m.get("sha") for r, m in core.build_manifest(sd or self.sd).items()}

    def invoke_turn(self, prompt="make slides", skill="demo"):
        return [u_prompt(prompt), a_tool("t1", "Skill", {"skill": skill}), u_result("t1", f"Launching skill: {skill}"),
                u_meta("Base directory for this skill: /x\n\n# SECRET SKILL BODY")]


class EnableTests(Base):
    def test_enable_stores_v1_and_leaves_skill_untouched(self):
        before = self.tree()
        res = core.enable(self.sd)
        self.assertEqual(res["version"], 1)
        self.assertFalse(res["already"])
        self.assertEqual(self.tree(), before, "enable must not modify the skill's own files")
        v1 = self.sd / "evolve" / "versions" / "v1"
        self.assertTrue((v1 / "SKILL.md.snapshot").is_file())
        self.assertTrue((v1 / "references" / "guide.md").is_file())
        self.assertEqual([p for p in (self.sd / "evolve").rglob("SKILL.md")], [], "no stored file may be named SKILL.md")
        st = core.load_state(self.sd)
        self.assertTrue(st["enabled"])
        self.assertFalse(st["locked"])
        self.assertIn("v1", (self.sd / "evolve" / "changelog.md").read_text())

    def test_enable_twice_is_idempotent(self):
        core.enable(self.sd)
        res = core.enable(self.sd)
        self.assertTrue(res["already"])
        self.assertEqual(res["version"], 1)
        self.assertEqual(len(core.list_versions(self.sd)), 1)

    def test_refuses_synced_and_plugin_skills(self):
        synced = self.skills / "synced" / "abc" / "pdf"
        synced.mkdir(parents=True)
        (synced / "SKILL.md").write_text("---\nname: pdf\n---\n")
        self.assertIn("synced", core.refuse_reason(synced.resolve()))
        with self.assertRaises(core.EvoError):
            core.enable(synced.resolve())
        plug = self.cfg / "plugins" / "cache" / "x" / "skills" / "y"
        plug.mkdir(parents=True)
        (plug / "SKILL.md").write_text("---\nname: y\n---\n")
        self.assertIn("plugin", core.refuse_reason(plug.resolve()))

    def test_p1_3_records_are_git_ignored_without_touching_the_skill(self):
        (self.sd / ".git").mkdir()
        before = self.tree()
        core.enable(self.sd)
        self.assertEqual(self.tree(), before, "enabling must not touch the skill's own files")
        self.assertFalse((self.sd / ".gitignore").exists())
        self.assertEqual((self.sd / "evolve" / ".gitignore").read_text(), "*\n")
        (self.sd / "evolve" / ".gitignore").unlink()             # a skill enabled by an older release
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        self.assertEqual((self.sd / "evolve" / ".gitignore").read_text(), "*\n")

    def test_p1_3_parent_git_repo_does_not_see_records(self):
        r = subprocess.run(["git", "init", "-q", str(self.cfg)], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("git not usable: " + r.stderr)
        core.enable(self.sd)
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records()), 1)
        out = subprocess.run(["git", "-C", str(self.cfg), "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True, text=True).stdout
        self.assertIn("skills/demo/SKILL.md", out)
        self.assertNotIn("evolve", out)

    def test_requested_snapshot_is_labelled_manual(self):
        core.enable(self.sd)
        (self.sd / "SKILL.md").write_text((self.sd / "SKILL.md").read_text() + "\nextra\n")
        self.assertEqual(evo.main(["snapshot", str(self.sd), "--note", "before big edit"]), 0)
        self.assertEqual(core.list_versions(self.sd)[-1]["actor"], "manual")
        self.assertIn("before big edit", (self.sd / "evolve" / "changelog.md").read_text())

    def test_resolve_by_frontmatter_name(self):
        sd2 = self.make_skill("demo-new", name="demo2")
        self.assertEqual(core.resolve_skill("demo2"), sd2)
        self.assertEqual(core.resolve_skill("demo-new"), sd2)
        self.assertEqual(core.resolve_skill(str(sd2)), sd2)
        with self.assertRaises(core.EvoError):
            core.resolve_skill("nope")


class RecordTests(Base):
    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    def test_nothing_recorded_until_skill_is_invoked(self):
        dump([u_prompt("hello"), a_text("hi there")], self.tp)
        self.run_hook()
        self.assertEqual(self.records(), [])

    def test_skill_tool_invocation_is_recorded_from_the_prompt(self):
        big = "X" * 200000
        dump(self.invoke_turn() + [
            a_tool("t2", "Read", {"file_path": str(self.sd / "references" / "guide.md")}), u_result("t2", "guide v1"),
            a_tool("t3", "Bash", {"command": "python build.py --all", "description": "Build the deck"}), u_result("t3", big),
            a_tool("t4", "Bash", {"command": "export.sh", "description": "Export preview"}),
            u_result("t4", "Exit code 1\nfont not found", is_error=True),
            a_text("Done. Slide 5 updated."),
            blob("user", "make slides"), blob("assistant", "Done. Slide 5 updated."),
        ], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("- skill: demo · v1", t)
        self.assertIn("- project: /proj", t)
        self.assertIn("## Turn 1", t)
        self.assertIn("> make slides", t)
        self.assertIn("- Skill · demo", t)
        self.assertIn("guide.md ◆skill-file", t)
        self.assertIn("- Bash · Build the deck", t)
        self.assertIn("- Bash · Export preview ❌", t)
        self.assertIn("font not found", t)
        self.assertIn("> Done. Slide 5 updated.", t)
        self.assertNotIn("XXXXXXXX", t, "tool output must be dropped")
        self.assertNotIn("SECRET SKILL BODY", t, "injected skill body must be dropped")
        self.assertEqual(t.count("make slides"), 1, "request-level copies must not be recorded twice")
        self.assertEqual(t.count("Done. Slide 5 updated."), 1)
        self.assertLess(len(t), 5000)

    def test_incremental_append_without_duplicates(self):
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        dump([a_text("first answer")], self.tp)
        self.run_hook()
        dump([u_prompt("make the font bigger"), a_text("font is 20 now")], self.tp)
        self.run_hook()
        self.run_hook()   # no new bytes
        t = self.record_text()
        self.assertEqual(t.count("## Turn 1"), 1)
        self.assertEqual(t.count("## Turn 2"), 1)
        self.assertEqual(t.count("first answer"), 1)
        self.assertEqual(t.count("font is 20 now"), 1)
        meta = json.loads(self.records()[0].with_suffix(".json").read_text())
        self.assertEqual(meta["turns"], 2)

    def test_partial_last_line_waits_for_next_run(self):
        dump(self.invoke_turn(), self.tp)
        with open(self.tp, "a") as f:
            f.write('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"half')
        self.run_hook()
        self.assertNotIn("half", self.record_text())
        with open(self.tp, "a") as f:
            f.write(' done"}]},"timestamp":"%s"}\n' % TS)
        self.run_hook()
        self.assertIn("> half done", self.record_text())

    def test_slash_invocation(self):
        dump([slash("demo", "quarterly review"), u_meta("Base directory for this skill: /x"), a_text("on it")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("> /demo quarterly review", t)
        self.assertIn("> on it", t)

    def test_builtin_commands_are_not_turns(self):
        dump([u_meta("<local-command-caveat>x</local-command-caveat>"),
              u_prompt("<command-name>/effort</command-name>\n<command-message>effort</command-message>\n<command-args></command-args>"),
              u_prompt("<local-command-stdout>Set effort level to high</local-command-stdout>")] + self.invoke_turn("real question"), self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("/effort", t)
        self.assertIn("## Turn 1", t)
        self.assertNotIn("## Turn 2", t)

    def test_builtin_command_split_across_two_hook_runs(self):
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        dump([u_prompt("<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args></command-args>")], self.tp)
        self.run_hook()                                   # the stdout marker has not been written yet
        dump([u_prompt("<local-command-stdout>Set model to x</local-command-stdout>"), u_prompt("next question"), a_text("next answer")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("/model", t)
        self.assertIn("> next question", t)
        self.assertEqual(t.count("## Turn"), 2)

    def test_trailing_skill_command_is_recorded_once_the_reply_arrives(self):
        dump([slash("demo", "weekly deck")], self.tp)
        self.run_hook()                                   # nothing after the command yet
        self.assertEqual(self.records(), [])
        dump([u_meta("Base directory for this skill: /x"), a_text("building it")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertEqual(t.count("> /demo weekly deck"), 1)
        self.assertIn("> building it", t)

    def test_system_written_messages_are_never_recorded_as_the_user(self):
        notif = u_prompt("<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n<summary>Background command \"Build deck\" completed (exit code 0)</summary>\n</task-notification>")
        notif.update(promptSource="system", origin={"kind": "task-notification"}, turnOrigin="task_notification")
        wrapped = u_prompt("<system-reminder>\n[SYSTEM NOTIFICATION - NOT USER INPUT]\n<task-notification><summary>Second job done</summary></task-notification>\n</system-reminder>")
        peer = u_prompt('Another Claude session sent a message:\n<teammate-message teammate_id="helper-1" color="green" summary="Chart is ready">\nSECRET PEER BODY\n</teammate-message>')
        human = u_prompt("thanks, now fix slide 3")
        human.update(promptSource="typed", origin={"kind": "human"}, turnOrigin="human")
        headless = u_prompt("run the weekly deck")
        headless.update(promptSource="sdk", turnOrigin="sdk")
        dump(self.invoke_turn() + [a_text("started"), notif, a_text("job finished"), wrapped, peer, a_text("got it"),
                                   human, a_text("fixed"), headless, a_text("done")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertEqual(t.count("## Turn"), 3, "only the three real prompts are turns")
        self.assertIn('- _(background task: Background command "Build deck" completed (exit code 0))_', t)
        self.assertIn("- _(background task: Second job done)_", t)
        self.assertIn("- _(message from another session (helper-1): Chart is ready)_", t)
        self.assertNotIn("SECRET PEER BODY", t)
        self.assertNotIn("> <task-notification", t)
        self.assertNotIn("> Another Claude session", t)
        self.assertIn("> thanks, now fix slide 3", t)
        self.assertIn("> run the weekly deck", t)

    def test_interrupt_and_reminder_and_image(self):
        dump(self.invoke_turn("fix it <system-reminder>hidden stuff</system-reminder>") + [
            {"type": "user", "timestamp": TS, "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
            {"type": "user", "timestamp": TS, "message": {"role": "user", "content": [{"type": "image", "source": {}}, {"type": "text", "text": "like this picture"}]}},
        ], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("hidden stuff", t)
        self.assertIn("(user interrupted)", t)
        self.assertIn("> [image]", t)
        self.assertIn("> like this picture", t)

    def test_spaced_json_and_sidechain(self):
        side = a_text("subagent chatter")
        side["isSidechain"] = True
        dump(self.invoke_turn() + [side, a_text("main answer")], self.tp, compact=False)
        self.run_hook()
        t = self.record_text()
        self.assertIn("main answer", t)
        self.assertNotIn("subagent chatter", t)

    def test_long_message_is_cut_with_a_note(self):
        dump(self.invoke_turn("A" * 50000), self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("more chars not copied", t)
        self.assertLess(len(t), 30000)

    def test_frontmatter_name_invocation(self):
        sd2 = self.make_skill("demo-new", name="other")
        core.enable(sd2)
        dump(self.invoke_turn(skill="other"), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records(sd2)), 1)
        self.assertEqual(self.records(), [])

    def test_two_skills_get_separate_records(self):
        sd2 = self.make_skill("second")
        core.enable(sd2)
        dump(self.invoke_turn() + [u_prompt("now the other"), a_tool("t9", "Skill", {"skill": "second"}), a_text("ok")], self.tp)
        self.run_hook()
        t1, t2 = self.record_text(), self.record_text(sd2)
        self.assertIn("make slides", t1)
        self.assertIn("now the other", t1)       # still the same window, still recorded
        self.assertNotIn("make slides", t2)      # second skill starts at its own prompt
        self.assertIn("now the other", t2)

    def test_disabled_skill_is_not_recorded(self):
        core.set_flag(self.sd, "enabled", False)
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        self.assertEqual(self.records(), [])

    def test_locked_skill_is_still_recorded(self):
        core.set_flag(self.sd, "locked", True)
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records()), 1)


class WindowTests(Base):
    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    def test_clear_in_same_window_continues_the_record_only_after_reinvoking(self):
        dump(self.invoke_turn("deck for Monday"), self.tp)
        self.run_hook("sess-1")
        tp2 = self.tmp / "sess-2.jsonl"                      # after /clear: new session, same window
        dump([u_prompt("unrelated question"), a_text("unrelated answer")], tp2)
        self.run_hook("sess-2", tp2)
        self.assertNotIn("unrelated", self.record_text())
        dump(self.invoke_turn("back to the deck"), tp2)
        self.run_hook("sess-2", tp2)
        t = self.record_text()                               # still exactly one record
        self.assertIn("new session in the same window", t)
        self.assertIn("deck for Monday", t)
        self.assertIn("back to the deck", t)
        self.assertIn("## Turn 2", t)
        self.assertNotIn("unrelated", t)

    def test_other_window_gets_its_own_record(self):
        dump(self.invoke_turn("deck A"), self.tp)
        self.run_hook("sess-1")
        os.environ["SKILL_EVOLUTION_WINDOW_KEY"] = "win-B"
        tp2 = self.tmp / "sess-9.jsonl"
        dump(self.invoke_turn("deck B"), tp2)
        self.run_hook("sess-9", tp2)
        self.assertEqual(len(self.records()), 2)

    def test_resumed_session_in_new_window_keeps_its_record(self):
        dump(self.invoke_turn("deck A"), self.tp)
        self.run_hook("sess-1")
        os.environ["SKILL_EVOLUTION_WINDOW_KEY"] = "win-C"   # claude --resume in another terminal
        dump([u_prompt("continue please"), a_text("continuing")], self.tp)
        self.run_hook("sess-1")
        t = self.record_text()
        self.assertIn("continue please", t)

    def test_window_key_falls_back_to_session(self):
        os.environ.pop("SKILL_EVOLUTION_WINDOW_KEY")
        k = hook.window_key("abc")
        self.assertTrue(k.startswith("w") or k == "s-abc")


class VersionTests(Base):
    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    def test_external_edit_becomes_a_new_version_at_use(self):
        (self.sd / "SKILL.md").write_text((self.sd / "SKILL.md").read_text() + "\nNew rule.\n")
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        st = core.load_state(self.sd)
        self.assertEqual(st["version"], 2)
        self.assertIn("- skill: demo · v2", self.record_text())
        cl = (self.sd / "evolve" / "changelog.md").read_text()
        self.assertIn("## v2", cl)
        self.assertIn("external", cl)
        self.assertIn("modified: SKILL.md", cl)
        (self.sd / "references" / "guide.md").write_text("guide v2\n")
        dump([u_prompt("again"), a_text("ok")], self.tp)
        self.run_hook()
        self.assertIn("[skill version: v2 → v3]", self.record_text())

    def test_touching_mtime_only_does_not_create_a_version(self):
        os.utime(self.sd / "SKILL.md", (1, 1))
        core.check_external(self.sd)
        self.assertEqual(core.load_state(self.sd)["version"], 1)

    def test_rollback_roundtrip_is_byte_identical_and_reversible(self):
        v1 = self.tree()
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nrewritten\n")
        (self.sd / "references" / "new.md").write_text("added\n")
        (self.sd / "scripts" / "run.py").unlink()
        (self.sd / "extra").mkdir()
        (self.sd / "extra" / "deep.md").write_text("deep\n")
        ver, created = core.snapshot(self.sd, "external", "manual")
        self.assertEqual((ver, created), (2, True))
        v2 = self.tree()
        res = core.rollback(self.sd, 1)
        self.assertEqual(res["version"], 3)
        self.assertEqual(self.tree(), v1)
        self.assertFalse((self.sd / "extra").exists(), "folders emptied by a rollback are removed")
        self.assertTrue((self.sd / "evolve" / "records").is_dir(), "evolve/ is never touched by a rollback")
        res = core.rollback(self.sd, 2)                       # undo the rollback
        self.assertEqual(res["version"], 4)
        self.assertEqual(self.tree(), v2)
        self.assertEqual([m["actor"] for m in core.list_versions(self.sd)], ["enable", "external", "rollback", "rollback"])

    def test_rollback_first_stores_unsaved_edits(self):
        (self.sd / "SKILL.md").write_text("unsaved work\n")
        res = core.rollback(self.sd, 1)
        self.assertEqual(res["captured"], 2)
        self.assertEqual(res["version"], 3)
        kept = (self.sd / "evolve" / "versions" / "v2" / "SKILL.md.snapshot").read_text()
        self.assertEqual(kept, "unsaved work\n")

    def test_rollback_to_current_content_is_a_noop(self):
        res = core.rollback(self.sd, 1)
        self.assertTrue(res["noop"])
        self.assertEqual(core.load_state(self.sd)["version"], 1)

    def test_rollback_to_missing_version_is_refused(self):
        with self.assertRaises(core.EvoError):
            core.rollback(self.sd, 7)

    def test_large_files_are_tracked_not_copied(self):
        big = self.sd / "assets"
        big.mkdir()
        (big / "font.bin").write_bytes(b"\0" * (core.LARGE_FILE + 10))
        ver, _ = core.snapshot(self.sd, "external", "big asset")
        self.assertFalse((self.sd / "evolve" / "versions" / f"v{ver}" / "assets" / "font.bin").exists())
        res = core.rollback(self.sd, 1)
        self.assertTrue((big / "font.bin").exists(), "a large file is never deleted by a rollback")
        self.assertTrue(any("font.bin" in w for w in res["warnings"]))


class HookInstallTests(Base):
    def test_install_merges_and_uninstall_removes_only_ours(self):
        sp = self.cfg / "settings.json"
        original = {"model": "x", "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "afplay ding.aiff"}]}],
                                            "PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "guard.py"}]}]}}
        sp.write_text(json.dumps(original, indent=4) + "\n")
        r = core.install_hooks()
        self.assertEqual(sorted(r["added"]), ["SessionEnd", "Stop"])
        self.assertTrue(Path(r["backup"]).is_file())
        data = json.loads(sp.read_text())
        self.assertEqual(data["model"], "x")
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "afplay ding.aiff")
        self.assertEqual(data["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
        self.assertIn('\n    "model"', sp.read_text(), "indentation style is kept")
        self.assertEqual(sorted(core.hooks_installed()), ["SessionEnd", "Stop"])
        self.assertEqual(core.install_hooks()["added"], [])            # idempotent
        r = core.uninstall_hooks()
        self.assertEqual(r["removed"], ["SessionEnd", "Stop"])
        self.assertEqual(json.loads(sp.read_text()), original)
        self.assertEqual(core.uninstall_hooks()["removed"], [])

    def test_install_without_settings_file(self):
        core.install_hooks()
        self.assertEqual(sorted(core.hooks_installed()), ["SessionEnd", "Stop"])

    def test_broken_settings_is_refused_not_overwritten(self):
        sp = self.cfg / "settings.json"
        sp.write_text("{ not json")
        with self.assertRaises(core.EvoError):
            core.install_hooks()
        self.assertEqual(sp.read_text(), "{ not json")


class ProcessTests(Base):
    """The hook as Claude Code runs it: a subprocess fed JSON on stdin."""

    def call(self, stdin, extra_env=None):
        env = dict(os.environ)
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(SCRIPTS / "record_hook.py")], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)

    def payload(self):
        return json.dumps({"session_id": "sess-1", "transcript_path": str(self.tp), "cwd": "/proj", "hook_event_name": "Stop"})

    def test_subprocess_records_and_is_silent(self):
        core.enable(self.sd)
        dump(self.invoke_turn(), self.tp)
        r = self.call(self.payload())
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(len(self.records()), 1)

    def test_background_marker_skips_everything(self):
        core.enable(self.sd)
        dump(self.invoke_turn(), self.tp)
        r = self.call(self.payload(), {core.BG_ENV: "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(self.records(), [])

    def test_garbage_input_never_fails(self):
        for stdin in ("", "not json", "{}", json.dumps({"session_id": "s", "transcript_path": "/nope"})):
            r = self.call(stdin)
            self.assertEqual((r.returncode, r.stdout), (0, ""), stdin)

    def test_no_enabled_skill_leaves_no_trace(self):
        dump(self.invoke_turn(), self.tp)
        self.call(self.payload())
        self.assertFalse((self.cfg / "skill-evolution-in-use").exists())


class CliTests(Base):
    def cli(self, *args):
        r = subprocess.run([sys.executable, str(SCRIPTS / "evo.py"), *args], capture_output=True, text=True,
                           env=dict(os.environ), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_enable_needs_skill_checkup(self):
        code, out = self.cli("enable", "demo")
        self.assertEqual(code, 3)
        self.assertIn("install-checkup", out)
        self.assertFalse((self.sd / "evolve").exists(), "nothing may be created when the dependency is missing")
        (self.skills / "skill-checkup").mkdir()
        (self.skills / "skill-checkup" / "SKILL.md").write_text("---\nname: skill-checkup\n---\n")
        code, out = self.cli("enable", "demo")
        self.assertEqual(code, 0, out)
        self.assertIn("enabled: demo · v1", out)
        self.assertIn("NEXT Claude Code session", out)
        self.assertIn("were not modified", out)

    def test_full_cli_cycle(self):
        self.assertEqual(self.cli("enable", "demo", "--skip-checkup")[0], 0)
        code, out = self.cli("status", "demo")
        self.assertEqual(code, 0)
        for needle in ("enabled · unlocked · v1", "matches v1", "0 file(s)", "installed (Stop, SessionEnd)"):
            self.assertIn(needle, out)
        (self.sd / "SKILL.md").write_text("broken\n")
        self.assertIn("differs from v1", self.cli("status", "demo")[1])
        code, out = self.cli("rollback", "demo", "v1")
        self.assertEqual(code, 0, out)
        self.assertIn("stored first as v2", out)
        self.assertIn("stored as v3", out)
        self.assertIn("name: demo", (self.sd / "SKILL.md").read_text())
        self.assertIn("locked: demo", self.cli("lock", "demo")[1])
        self.assertIn("LOCKED", self.cli("status", "demo")[1])
        self.assertIn("unlocked: demo", self.cli("unlock", "demo")[1])
        self.assertIn("v3", self.cli("versions", "demo")[1])
        self.assertIn("disabled: demo", self.cli("disable", "demo")[1])
        self.assertIn("no skill has evolution enabled", self.cli("status")[1])
        self.assertTrue((self.sd / "evolve" / "versions" / "v1").is_dir(), "disable keeps everything")

    def test_status_surfaces_silent_recorder_errors(self):
        self.cli("enable", "demo", "--skip-checkup")
        self.assertNotIn("WARNING", self.cli("status", "demo")[1])
        log = self.cfg / "skill-evolution-in-use" / "run" / "hook.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("2026-11-15T22:00:00+09:00 ERROR Traceback (most recent call last):\n  File x\nValueError: boom\n")
        out = self.cli("status", "demo")[1]
        self.assertIn("WARNING", out)
        self.assertIn("1 recorder error(s), last at 2026-11-15 22:00:00", out)

    def test_recorder_failure_is_logged_not_raised(self):
        core.enable(self.sd)
        dump(self.invoke_turn(), self.tp)
        (self.sd / "evolve" / "records").rmdir()
        (self.sd / "evolve" / "records").write_text("a file where the folder should be")   # forces an OSError inside the hook
        r = subprocess.run([sys.executable, str(SCRIPTS / "record_hook.py")], capture_output=True, text=True, env=dict(os.environ),
                           input=json.dumps({"session_id": "sess-1", "transcript_path": str(self.tp), "hook_event_name": "Stop"}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertIn("WARNING", self.cli("status", "demo")[1])

    def test_refusals_exit_2(self):
        self.assertEqual(self.cli("enable", "ghost", "--skip-checkup")[0], 2)
        self.assertEqual(self.cli("rollback", "demo", "v1")[0], 2)          # never enabled
        self.cli("enable", "demo", "--skip-checkup")
        self.assertEqual(self.cli("rollback", "demo", "banana")[0], 2)
        self.assertEqual(self.cli("rollback", "demo", "v9")[0], 2)


class CheckupInstallTests(Base):
    """install_checkup without the network: the two fetchers are replaced."""

    def setUp(self):
        super().setUp()
        self._git, self._tar = core._fetch_checkup_git, core._fetch_checkup_tarball

    def tearDown(self):
        core._fetch_checkup_git, core._fetch_checkup_tarball = self._git, self._tar
        super().tearDown()

    @staticmethod
    def good(tmp):
        tmp.mkdir(parents=True)
        (tmp / "SKILL.md").write_text("---\nname: skill-checkup\n---\n")

    @staticmethod
    def stalls(tmp):
        tmp.mkdir(parents=True)                     # git creates the folder, then hangs and is killed
        raise subprocess.TimeoutExpired("git clone", 30)

    def leftovers(self):
        return sorted(p.name for p in self.skills.iterdir() if "installing" in p.name)

    def test_download_is_tried_first(self):
        core._fetch_checkup_tarball = self.good
        core._fetch_checkup_git = lambda tmp: self.fail("git must not run when the download worked")
        self.assertEqual(core.install_checkup(), "installed (download)")
        self.assertTrue(core.checkup_present())
        self.assertEqual(core.install_checkup(), "present")

    def test_falls_back_to_git_when_download_fails(self):
        core._fetch_checkup_tarball, core._fetch_checkup_git = self.stalls, self.good
        self.assertEqual(core.install_checkup(), "installed (git)")
        self.assertTrue(core.checkup_present())
        self.assertEqual(self.leftovers(), [])

    def test_empty_leftover_folder_does_not_block_a_retry(self):
        (self.skills / "skill-checkup").mkdir()
        core._fetch_checkup_tarball = self.good
        self.assertEqual(core.install_checkup(), "installed (download)")

    def test_both_ways_failing_reports_both_and_leaves_nothing(self):
        core._fetch_checkup_git = core._fetch_checkup_tarball = self.stalls
        with self.assertRaises(core.EvoError) as cm:
            core.install_checkup()
        self.assertIn("git:", str(cm.exception))
        self.assertIn("download:", str(cm.exception))
        self.assertFalse((self.skills / "skill-checkup").exists())
        self.assertEqual(self.leftovers(), [])

    def test_fetched_folder_without_skill_md_is_rejected(self):
        def bad(tmp):
            tmp.mkdir(parents=True)
            (tmp / "README.md").write_text("x")
        core._fetch_checkup_git = core._fetch_checkup_tarball = bad
        with self.assertRaises(core.EvoError):
            core.install_checkup()
        self.assertFalse(core.checkup_present())

    def test_foreign_non_empty_folder_is_never_replaced(self):
        d = self.skills / "skill-checkup"
        d.mkdir()
        (d / "notes.txt").write_text("mine")
        core._fetch_checkup_tarball = core._fetch_checkup_git = self.good
        with self.assertRaises(core.EvoError):
            core.install_checkup()
        self.assertEqual((d / "notes.txt").read_text(), "mine")

# ---------------------------------------------------------------- fixes from the 2026-11-15 audit
# Test names start with the fix-list ID they cover.


class SnapshotSafetyTests(Base):
    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    def test_p0_1_interrupted_snapshot_does_not_wedge_versioning(self):
        debris = self.sd / "evolve" / "versions" / "v2"
        debris.mkdir()
        (debris / "half.md").write_text("partial copy")          # killed before manifest.json was written
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nedited\n")
        self.assertEqual(core.snapshot(self.sd, "manual", "after a crash"), (2, True))
        self.assertTrue((self.sd / "evolve" / "versions" / "v2" / "manifest.json").is_file())
        aside = [p for p in (self.sd / "evolve" / "versions").iterdir() if p.name.startswith(".v2.")]
        self.assertEqual(len(aside), 1, "debris is moved aside, never deleted")
        self.assertEqual((aside[0] / "half.md").read_text(), "partial copy")
        self.assertEqual([m["version"] for m in core.list_versions(self.sd)], [1, 2])

    def test_p0_1_stored_version_missing_from_state_is_skipped_not_fatal(self):
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nedit A\n")
        core.snapshot(self.sd, "manual", "A")
        st = core.load_state(self.sd)
        st["version"] = 1                                        # killed after v2 was in place, before state.json
        core.save_state(self.sd, st)
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nedit B\n")
        self.assertEqual(core.snapshot(self.sd, "manual", "B"), (3, True))

    def test_p0_1_refused_rollback_leaves_live_files_untouched(self):
        (self.sd / "evolve" / "versions" / "v1" / "references" / "guide.md").unlink()   # a stored copy is lost
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nmy unsaved work\n")
        before = self.tree()
        with self.assertRaises(core.EvoError) as cm:
            core.rollback(self.sd, 1)
        self.assertIn("guide.md", str(cm.exception))
        self.assertEqual(self.tree(), before, "a refused rollback must not touch live files")
        self.assertEqual(len(core.list_versions(self.sd)), 1)

    def test_p0_1_lock_of_a_dead_process_is_broken_at_once(self):
        (self.sd / "evolve" / ".lock").write_text(str(dead_pid()))
        t0 = time.time()
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nedited\n")
        self.assertEqual(core.snapshot(self.sd, "manual", "x"), (2, True))
        self.assertLess(time.time() - t0, 2.0)
        (self.sd / "evolve" / ".lock").write_text(str(os.getpid()))      # a live holder still blocks
        with self.assertRaises(core.EvoError):
            with core.locked(self.sd, wait=0.2):
                pass

    def test_p0_1_hook_logs_a_skipped_version_capture_and_still_records(self):
        (self.sd / "SKILL.md").write_text((self.sd / "SKILL.md").read_text() + "\nedited\n")
        (self.sd / "evolve" / ".lock").write_text(str(os.getpid()))      # capture cannot get the lock
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records()), 1, "recording goes on")
        self.assertIn("version capture", self.log_text())

    def test_p0_3_rollback_keeps_a_live_file_that_grew_past_the_size_limit(self):
        (self.sd / "assets").mkdir()
        (self.sd / "assets" / "data.bin").write_bytes(b"small")
        core.snapshot(self.sd, "manual", "small data")                   # v2 holds the small copy
        (self.sd / "assets" / "data.bin").write_bytes(b"\1" * (core.LARGE_FILE + 10))   # too large to store
        res = core.rollback(self.sd, 2)
        self.assertEqual((self.sd / "assets" / "data.bin").stat().st_size, core.LARGE_FILE + 10,
                         "the only copy of a large file is never overwritten")
        self.assertTrue(any("data.bin" in w for w in res["warnings"]))

    def test_p2_1_user_rollback_is_allowed_on_a_locked_skill(self):
        core.set_flag(self.sd, "locked", True)
        (self.sd / "SKILL.md").write_text("broken\n")
        self.assertFalse(core.rollback(self.sd, 1)["noop"])
        self.assertIn("name: demo", (self.sd / "SKILL.md").read_text())

    def test_p2_2_rollback_prev(self):
        (self.sd / "SKILL.md").write_text("---\nname: demo\n---\nv2 text\n")
        core.snapshot(self.sd, "manual", "v2")
        (self.sd / "SKILL.md").write_text("unsaved\n")
        res = core.rollback(self.sd, "prev")                  # unsaved edits: back to the current stored version
        self.assertEqual((res["captured"], res["restored"], res["version"]), (3, 2, 4))
        self.assertIn("v2 text", (self.sd / "SKILL.md").read_text())
        out = []
        with unittest.mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(evo.main(["rollback", "demo", "prev"]), 0)   # clean: back one version (undo)
        self.assertEqual(core.load_state(self.sd)["version"], 5)
        self.assertEqual((self.sd / "SKILL.md").read_text(), "unsaved\n")
        with self.assertRaises(core.EvoError):
            sd2 = self.make_skill("fresh")
            core.enable(sd2)
            core.rollback(sd2, "prev")                         # v1, nothing earlier

    def test_p2_4_folder_standing_where_a_file_must_go_is_replaced(self):
        v1 = self.tree()
        (self.sd / "references" / "guide.md").unlink()
        (self.sd / "references" / "guide.md").mkdir()
        (self.sd / "references" / "guide.md" / "inner.md").write_text("inner\n")
        res = core.rollback(self.sd, 1)
        self.assertEqual(self.tree(), v1)
        self.assertEqual(res["mismatch"], [])

    def test_p2_4_symlink_in_the_way_is_removed_not_written_through(self):
        v1 = self.tree()
        shared = self.tmp / "shared-refs"
        shared.mkdir()
        (shared / "guide.md").write_text("someone else's file\n")
        shutil.rmtree(self.sd / "references")
        (self.sd / "references").symlink_to(shared, target_is_directory=True)
        res = core.rollback(self.sd, 1)
        self.assertEqual((shared / "guide.md").read_text(), "someone else's file\n", "never write through a link")
        self.assertFalse((self.sd / "references").is_symlink())
        self.assertEqual(self.tree(), v1)
        self.assertTrue(any("removed references" in w for w in res["warnings"]))


class AuditRecordTests(Base):
    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    def test_p0_2_skill_enabled_by_path_outside_the_skills_root_is_recorded(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        ext = self.make_skill("ext-skill", parent=outside)
        core.enable(ext)
        self.assertIn(ext, core.enabled_skill_dirs())
        dump(self.invoke_turn(skill="ext-skill"), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records(ext)), 1)
        core.set_flag(ext, "enabled", False)
        self.assertNotIn(ext, core.enabled_skill_dirs())

    def test_p1_1_invocations_before_enabling_are_not_recorded(self):
        old = self.invoke_turn("old request") + [a_text("old answer")]
        for e in old:
            e["timestamp"] = OLD_TS
        dump(old, self.tp)
        self.run_hook()
        self.assertEqual(self.records(), [])
        dump(self.invoke_turn("new request") + [a_text("new answer")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("old request", t)
        self.assertIn("> new request", t)

    def test_p1_2_one_failing_skill_neither_duplicates_nor_stops_the_others(self):
        sd2 = self.make_skill("second")
        core.enable(sd2)
        recs = self.sd / "evolve" / "records"
        shutil.rmtree(recs)
        recs.write_text("blocker")                              # demo cannot write its record
        dump(self.invoke_turn() + [u_prompt("now the other"), a_tool("t9", "Skill", {"skill": "second"}), a_text("ok")], self.tp)
        self.run_hook()
        dump([u_prompt("third prompt"), a_text("third answer")], self.tp)
        self.run_hook()
        t2 = self.record_text(sd2)
        self.assertEqual(t2.count("now the other"), 1)
        self.assertEqual(t2.count("third prompt"), 1)
        self.assertIn("ERROR", self.log_text())
        recs.unlink()                                           # demo's problem is gone
        dump([u_prompt("fourth prompt"), a_text("fourth answer")], self.tp)
        self.run_hook()
        t1 = self.record_text()
        for s in ("make slides", "now the other", "third prompt", "fourth prompt"):
            self.assertEqual(t1.count(s), 1, s)
        self.assertEqual(self.record_text(sd2).count("third prompt"), 1)

    def test_p2_5_quoted_tags_in_a_real_message_keep_their_text(self):
        msgs = ["why does <command-name>/demo</command-name> show up in the log?",
                "the hook prints <task-notification> blocks, ignore them",
                'what is <teammate-message teammate_id="x"> for?']
        entries = self.invoke_turn("start")
        for m in msgs:
            entries += [u_prompt(m), a_text("answer")]
        dump(entries, self.tp)
        self.run_hook()
        t = self.record_text()
        for m in msgs:
            self.assertIn("> " + m, t)
        self.assertEqual(t.count("## Turn"), 4)

    def test_p2_5_quoted_command_is_not_an_invocation(self):
        dump([u_prompt("explain <command-name>/demo</command-name> to me"), a_text("sure")], self.tp)
        self.run_hook()
        self.assertEqual(self.records(), [])

    def lock(self):
        lp = self.cfg / "skill-evolution-in-use" / "run" / "sessions" / "sess-1.lock"
        lp.parent.mkdir(parents=True, exist_ok=True)
        return lp

    def test_p2_6_stale_session_lock_is_taken_over_in_the_same_run(self):
        self.lock().write_text(str(dead_pid()))                 # holder died
        dump(self.invoke_turn("first"), self.tp)
        self.run_hook()
        self.assertIn("> first", self.record_text())
        self.lock().write_text("")                              # old lock without a PID
        os.utime(self.lock(), (1, 1))
        dump([u_prompt("second"), a_text("ok")], self.tp)
        self.run_hook()
        self.assertIn("> second", self.record_text())

    def test_p2_6_run_skipped_by_a_live_lock_is_logged(self):
        saved = getattr(hook, "SESSION_LOCK_WAIT", None)
        hook.SESSION_LOCK_WAIT = 0.1
        try:
            self.lock().write_text(str(os.getpid()))
            dump(self.invoke_turn(), self.tp)
            self.run_hook()
        finally:
            hook.SESSION_LOCK_WAIT = saved
        self.assertEqual(self.records(), [])
        self.assertIn("SKIP", self.log_text())

    def test_p3_2_builtin_command_marked_by_a_system_entry_is_not_a_turn(self):
        dump(self.invoke_turn(), self.tp)
        self.run_hook()
        dump([u_prompt("<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args></command-args>"),
              {"type": "system", "subtype": "local_command", "content": "<local-command-stdout>Set model to x</local-command-stdout>",
               "level": "info", "timestamp": TS, "isMeta": False},
              u_prompt("next question"), a_text("next answer")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("/model", t)
        self.assertIn("> next question", t)
        self.assertEqual(t.count("## Turn"), 2)

    def test_p3_3_skill_file_mark_is_exact(self):
        sib = self.make_skill("demo-new")
        self.assertTrue(hook.touches_skill({"file_path": str(self.sd / "SKILL.md")}, self.sd))
        self.assertFalse(hook.touches_skill({"file_path": str(sib / "SKILL.md")}, self.sd), "a sibling with the same prefix")
        self.assertFalse(hook.touches_skill({"file_path": str(self.sd / "evolve" / "records" / "x.md")}, self.sd),
                         "evolve/ is data, not the skill")
        os.environ["HOME"] = str(self.tmp)
        rel = self.sd.relative_to(self.tmp).as_posix()
        self.assertTrue(hook.touches_skill({"command": f"cat ~/{rel}/SKILL.md", "description": "Show it"}, self.sd))
        self.assertTrue(hook.touches_skill({"command": f'sed -n 1,5p "$HOME/{rel}/references/guide.md"'}, self.sd))
        self.assertFalse(hook.touches_skill({"command": f"ls ~/{rel}-new/"}, self.sd))

    def test_p3_7_renamed_skill_is_still_recorded(self):
        text = (self.sd / "SKILL.md").read_text().replace("name: demo", "name: deck-maker")
        (self.sd / "SKILL.md").write_text(text)
        dump(self.invoke_turn(skill="deck-maker"), self.tp)
        self.run_hook()
        self.assertEqual(len(self.records()), 1)

    def test_r1_headless_session_is_labelled(self):
        entries = self.invoke_turn("run the weekly deck") + [a_text("done")]
        for e in entries:
            e["entrypoint"] = "sdk-cli"
        dump(entries, self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("- entrypoint: sdk-cli", t)
        self.assertIn("**Prompt (non-interactive)**\n\n> run the weekly deck", t)
        self.assertNotIn("**User**", t)

    def test_r1_interactive_session_keeps_the_user_label(self):
        entries = self.invoke_turn("make it blue") + [a_text("done")]
        for e in entries:
            e["entrypoint"] = "cli"
        dump(entries, self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("- entrypoint: cli", t)
        self.assertIn("**User**\n\n> make it blue", t)

    def test_r2_warning_lines_of_successful_tools_are_kept(self):
        out = "\n".join([f"line {i} fine" for i in range(50)] + [
            "WARNING: no .claude/skills under /x", "No eval directories found in /y",
            "Traceback (most recent call last):", "PermissionError: denied"])
        long_line = "error " + "y" * 400
        dump(self.invoke_turn() + [
            a_tool("t5", "Bash", {"command": "scan", "description": "Scan skills"}), u_result("t5", out),
            a_tool("t6", "Bash", {"command": "check", "description": "Check one"}), u_result("t6", long_line),
            a_tool("t7", "Read", {"file_path": "/x/notes.md"}), u_result("t7", "1\tHandle the error case"),
            a_text("done")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("- Bash · Scan skills\n  ⚠ WARNING: no .claude/skills under /x\n"
                      "  ⚠ No eval directories found in /y\n  ⚠ Traceback (most recent call last):\n", t)
        self.assertNotIn("PermissionError", t, "at most 3 lines")
        self.assertNotIn("line 3 fine", t)
        self.assertIn("  ⚠ " + long_line[:200] + "\n", t)
        self.assertNotIn(long_line[:201], t)
        self.assertNotIn("Handle the error case", t, "file contents are not tool status")

    def test_r4_message_typed_mid_turn_is_recorded_as_the_user(self):
        hook_ok = {"type": "attachment", "isSidechain": False, "timestamp": TS,
                   "attachment": {"type": "hook_success", "hookEvent": "UserPromptSubmit", "content": "HOOK TEXT"}}
        img = queued([{"type": "text", "text": "[Image #3] 感觉没居中"}, {"type": "image", "source": {}}])
        dump(self.invoke_turn() + [
            a_tool("t5", "Bash", {"command": "draw", "description": "Draw"}),
            queued("跑一个小时就停"), hook_ok, u_result("t5", "ok"), img,
            queued('<agent-message from="x">PEER REPORT</agent-message>', kind="peer"),
            queued("SIDECHAIN TEXT", sidechain=True), a_text("stopping")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("**User** _(sent while Claude was working)_\n\n> 跑一个小时就停\n", t)
        self.assertIn("> [Image #3] 感觉没居中\n> [image]", t)
        for gone in ("PEER REPORT", "SIDECHAIN TEXT", "HOOK TEXT"):
            self.assertNotIn(gone, t)
        self.assertEqual(t.count("## Turn"), 1, "a mid-turn message is not a new turn")

    def test_r4_message_relayed_by_a_plugin_as_the_user_is_the_user(self):
        m = u_prompt("现在啥情况")
        m.update(origin={"kind": "plugin", "name": "clawd-pet", "asUser": True}, promptSource="system",
                 turnOrigin="system", queuePriority="later")
        enqueue = {"type": "queue-operation", "operation": "enqueue", "timestamp": TS, "content": "现在啥情况"}
        dump(self.invoke_turn() + [a_text("working"), enqueue, m, a_text("status: fine")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("## Turn 2", t)
        self.assertIn("**User**\n\n> 现在啥情况", t)
        self.assertNotIn("system message", t)
        self.assertEqual(t.count("现在啥情况"), 1)


class AuditCliTests(Base):
    cli = CliTests.cli

    def test_p1_1_enable_says_when_recording_starts(self):
        out = self.cli("enable", "demo", "--skip-checkup")[1]
        self.assertIn("NEXT Claude Code session", out)
        self.make_skill("second")
        out = self.cli("enable", "second", "--skip-checkup")[1]
        self.assertIn("next time the skill is invoked", out)
        self.assertIn("already open", out)

    def test_p2_4_restore_that_cannot_complete_exits_2(self):
        self.cli("enable", "demo", "--skip-checkup")
        shutil.rmtree(self.sd / "references")
        (self.sd / "references").write_bytes(b"\1" * (core.LARGE_FILE + 10))   # a large file where v1 has a folder
        code, out = self.cli("rollback", "demo", "v1")
        self.assertEqual(code, 2, out)
        self.assertIn("references/guide.md", out)
        self.assertNotIn("Traceback", out)
        self.assertTrue((self.sd / "references").is_file(), "the large file is kept")

    def test_p3_1_hooks_from_a_differently_named_folder_are_found(self):
        copy = self.tmp / "my-recorder" / "scripts"
        shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns("__pycache__"))

        def run(*a):
            return subprocess.run([sys.executable, str(copy / "evo.py"), *a], capture_output=True, text=True,
                                  env=dict(os.environ), timeout=60)
        run("install-hooks")
        run("install-hooks")
        data = json.loads((self.cfg / "settings.json").read_text())
        for ev in ("Stop", "SessionEnd"):
            cmds = [h["command"] for g in data["hooks"][ev] for h in g["hooks"]]
            self.assertEqual(len(cmds), 1, cmds)
        self.assertIn("removed", run("uninstall-hooks").stdout)
        self.assertEqual(json.loads((self.cfg / "settings.json").read_text()).get("hooks", {}), {})

    def test_p3_4_os_errors_exit_2_with_a_plain_message(self):
        (self.sd / "evolve").write_text("a file where the data folder should be")
        code, out = self.cli("enable", "demo", "--skip-checkup", "--no-hooks")
        self.assertEqual(code, 2, out)
        self.assertIn("failed", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.cli("rollback")[0], 2, "bad arguments exit 2 too")

    def test_p3_5_invalid_settings_refuses_before_enabling(self):
        (self.cfg / "settings.json").write_text("{ not json")
        code, out = self.cli("enable", "demo", "--skip-checkup")
        self.assertEqual(code, 2, out)
        self.assertFalse((self.sd / "evolve").exists(), "nothing is enabled when settings.json is broken")

    def test_p3_6_enable_message_is_true_for_a_git_skill(self):
        (self.sd / ".git").mkdir()
        before = self.tree()
        code, out = self.cli("enable", "demo", "--skip-checkup")
        self.assertEqual(code, 0, out)
        self.assertIn("were not modified", out)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.sd / ".gitignore").exists())


class AuditHookInstallTests(Base):
    def test_p3_5_settings_symlink_stays_a_symlink(self):
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"model": "x"}\n')
        sp = self.cfg / "settings.json"
        sp.symlink_to(real)
        core.install_hooks()
        self.assertTrue(sp.is_symlink())
        self.assertIn("Stop", json.loads(real.read_text())["hooks"])
        core.uninstall_hooks()
        self.assertTrue(sp.is_symlink())
        self.assertEqual(json.loads(real.read_text())["model"], "x")


class AuditShellTests(Base):
    def test_p3_1_installed_command_with_its_marker_runs_under_a_shell(self):
        core.install_hooks()
        cmd = json.loads((self.cfg / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.endswith(core.HOOK_MARK), cmd)
        core.enable(self.sd)
        dump(self.invoke_turn(), self.tp)
        payload = json.dumps({"session_id": "sess-1", "transcript_path": str(self.tp), "cwd": "/proj", "hook_event_name": "Stop"})
        r = subprocess.run(["/bin/sh", "-c", cmd], input=payload, capture_output=True, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(len(self.records()), 1)


class DocTests(unittest.TestCase):
    """SKILL.md claims that the fixes made true, or that were false before."""

    def setUp(self):
        self.text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.desc = re.search(r"(?m)^description:(.*)$", self.text).group(1)
        self.body = self.text.split("\n---\n", 1)[1]

    def test_p2_8_description_leaves_yue_yong_yue_hao_to_skills_evolve(self):
        self.assertNotIn("越用越好", self.desc)

    def test_p2_7_hand_roll_clause_is_scoped_to_enabled_skills(self):
        self.assertRegex(self.desc, r"(?i)evolution enabled[^.;]*hand-roll")
        self.assertRegex(self.body, r"(?i)never enable[^.]*own initiative")

    def test_p2_3_failed_checkup_fetch_has_a_way_forward(self):
        self.assertIn("--skip-checkup", self.body)

    def test_p0_3_rollback_is_not_called_always_reversible(self):
        self.assertNotIn("reversibility is built in", self.body)
        self.assertRegex(self.body, r"(?i)2 MB[^.]*never overwrit")

    def test_p1_1_step_4_states_both_start_points(self):
        self.assertRegex(self.body, r"next Claude Code\s+session")
        self.assertRegex(self.body, r"already\s+open")

    def test_p2_1_lock_allows_a_requested_rollback(self):
        self.assertRegex(self.body, r"(?i)rollback the user asks for[^.]*locked")

    def test_p2_2_undo_maps_to_prev(self):
        self.assertRegex(self.body, r"退回上一版[^\n]*\| `rollback <skill> prev`")

    def test_p3_4_exit_2_covers_bad_arguments(self):
        self.assertRegex(self.body, r"(?i)exit 2[^.]*bad arguments")

    def test_p3_8_edits_between_two_uses_become_one_version(self):
        self.assertRegex(self.body, r"(?i)between two uses[^.]*one version")

    def test_size_and_skill_checkup_checkers(self):
        self.assertLess(len(self.text), 6000)
        sc = Path.home() / ".claude/skills/skill-checkup/scripts"
        if not sc.is_dir():
            self.skipTest("skill-checkup not installed")
        for script in ("check_routes.py", "check_script_safety.py"):
            r = subprocess.run([sys.executable, str(sc / script), str(SKILL)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("PASS", r.stdout)


class QueueOperationTests(Base):
    """Older Claude Code stores a message typed while Claude works ONLY as queue-operation entries."""

    def setUp(self):
        super().setUp()
        core.enable(self.sd)

    @staticmethod
    def q(op, content=None, **kw):
        d = {"type": "queue-operation", "operation": op, "timestamp": TS}
        if content is not None:
            d["content"] = content
        d.update(kw)
        return d

    def test_queued_message_stored_only_as_queue_operation_is_recorded(self):
        dump(self.invoke_turn() + [self.q("enqueue", "also make the title bigger"), a_text("deck built"),
                                   self.q("dequeue"), a_text("title is bigger now")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertIn("> also make the title bigger", t)
        self.assertEqual(t.count("## Turn"), 2)

    def test_queued_message_with_a_user_entry_is_not_duplicated(self):
        dump(self.invoke_turn() + [self.q("enqueue", "next slide please"), a_text("done"),
                                   self.q("dequeue"), u_prompt("next slide please"), a_text("ok")], self.tp)
        self.run_hook()
        self.assertEqual(self.record_text().count("next slide please"), 1)

    def test_enqueue_and_dequeue_split_across_hook_runs(self):
        dump(self.invoke_turn() + [self.q("enqueue", "split message"), a_text("first reply")], self.tp)
        self.run_hook()
        dump([self.q("dequeue"), a_text("second reply")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertEqual(t.count("> split message"), 1)
        self.assertIn("## Turn 2", t)

    def test_absorbed_mid_turn_is_recorded_once_through_its_attachment(self):
        att = {"type": "attachment", "timestamp": TS, "attachment": {"type": "queued_command", "prompt": "absorbed text", "origin": {"kind": "human"}}}
        dump(self.invoke_turn() + [self.q("enqueue", "absorbed text"), self.q("remove", "absorbed text", reason="absorbed_mid_turn"), att,
                                   a_text("handled"), self.q("dequeue")], self.tp)
        self.run_hook()
        self.assertEqual(self.record_text().count("absorbed text"), 1)

    def test_queued_builtin_command_is_not_a_turn_and_system_text_is_not_queued(self):
        dump(self.invoke_turn() + [self.q("enqueue", "/model"), self.q("enqueue", '<agent-message from="x">done</agent-message>'), a_text("r1"),
                                   self.q("dequeue"), u_prompt("<local-command-stdout>Set model to x</local-command-stdout>"),
                                   self.q("dequeue"), u_prompt("real follow-up"), a_text("r2")], self.tp)
        self.run_hook()
        t = self.record_text()
        self.assertNotIn("/model", t)
        self.assertNotIn("agent-message", t)
        self.assertIn("> real follow-up", t)


if __name__ == "__main__":
    unittest.main(verbosity=1)
