#!/usr/bin/env python3
"""Unit tests for agy-gemini/agy_run.py."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE_PATH = Path(__file__).resolve().parent.parent / "agy-gemini" / "agy_run.py"
spec = importlib.util.spec_from_file_location("agy_run", MODULE_PATH)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class TestParseBlocks(unittest.TestCase):
    def test_one_normal_block(self):
        text = (
            "FILE: a.py\n"
            "<<<<<<< SEARCH\n"
            "old line\n"
            "=======\n"
            "new line\n"
            ">>>>>>> REPLACE\n"
        )
        blocks, notes, errors = r.parse_blocks(text)
        self.assertEqual(errors, [])
        self.assertEqual(blocks, [("a.py", ["old line"], ["new line"])])
        self.assertEqual(notes, "")

    def test_file_line_variants(self):
        variants = ["FILE: a.py", "**FILE: a.py**", "### FILE: `a.py`"]
        for header in variants:
            with self.subTest(header=header):
                text = (
                    f"{header}\n"
                    "<<<<<<< SEARCH\n"
                    "old\n"
                    "=======\n"
                    "new\n"
                    ">>>>>>> REPLACE\n"
                )
                blocks, notes, errors = r.parse_blocks(text)
                self.assertEqual(errors, [])
                self.assertEqual(len(blocks), 1)
                self.assertEqual(blocks[0][0], "a.py")

    def test_notes_outside_blocks_returned(self):
        text = (
            "Header note\n"
            "FILE: a.py\n"
            "<<<<<<< SEARCH\n"
            "old\n"
            "=======\n"
            "new\n"
            ">>>>>>> REPLACE\n"
            "Footer note\n"
        )
        blocks, notes, errors = r.parse_blocks(text)
        self.assertEqual(errors, [])
        self.assertIn("Header note", notes)
        self.assertIn("Footer note", notes)

    def test_fence_lines_dropped_from_notes(self):
        text = (
            "```python\n"
            "code line\n"
            "```\n"
            "plain note\n"
        )
        blocks, notes, errors = r.parse_blocks(text)
        self.assertEqual(errors, [])
        self.assertNotIn("```", notes)
        self.assertIn("code line", notes)
        self.assertIn("plain note", notes)

    def test_unclosed_block_error(self):
        text = (
            "FILE: a.py\n"
            "<<<<<<< SEARCH\n"
            "old\n"
            "=======\n"
            "new\n"
        )
        blocks, notes, errors = r.parse_blocks(text)
        self.assertEqual(errors, ["An edit block is not closed with '>>>>>>> REPLACE'."])

    def test_block_without_file_error(self):
        text = (
            "<<<<<<< SEARCH\n"
            "old\n"
            "=======\n"
            "new\n"
            ">>>>>>> REPLACE\n"
        )
        blocks, notes, errors = r.parse_blocks(text)
        self.assertEqual(errors, ["An edit block has no 'FILE:' line before it."])


class TestReplaceOnce(unittest.TestCase):
    def test_exact_match(self):
        content = "first\nsecond\nthird\n"
        new_content, err = r.replace_once(content, ["second"], ["replaced"])
        self.assertIsNone(err)
        self.assertEqual(new_content, "first\nreplaced\nthird\n")

    def test_match_ignores_trailing_spaces(self):
        content = "line1  \nline2\n"
        new_content, err = r.replace_once(content, ["line1"], ["line1_new"])
        self.assertIsNone(err)
        self.assertEqual(new_content, "line1_new\nline2\n")

    def test_search_appears_twice_error(self):
        content = "match\nmiddle\nmatch\n"
        new_content, err = r.replace_once(content, ["match"], ["replaced"])
        self.assertIsNone(new_content)
        self.assertEqual(err, "the SEARCH part appears 2 times; it must appear once")

    def test_search_not_found_error(self):
        content = "alpha\nbeta\n"
        new_content, err = r.replace_once(content, ["gamma"], ["replaced"])
        self.assertIsNone(new_content)
        self.assertEqual(err, "the SEARCH part was not found in the file")

    def test_crlf_file_keeps_crlf(self):
        content = "first\r\nsecond\r\nthird\r\n"
        new_content, err = r.replace_once(content, ["second"], ["replaced"])
        self.assertIsNone(err)
        self.assertEqual(new_content, "first\r\nreplaced\r\nthird\r\n")


class TestBuildProposal(unittest.TestCase):
    def test_new_file_empty_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = {"sent": {}}
            blocks = [("new_file.py", [], ["print('hello')"])]
            files, errors = r.build_proposal(root, meta, blocks)
            self.assertEqual(errors, [])
            self.assertIn("new_file.py", files)
            self.assertIsNone(files["new_file.py"]["old"])
            self.assertEqual(files["new_file.py"]["new"], "print('hello')\n")

    def test_edit_file_in_meta_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            meta = {"sent": {"app.py": "dummy-sha"}}
            blocks = [("app.py", ["x = 1"], ["x = 2"])]
            files, errors = r.build_proposal(root, meta, blocks)
            self.assertEqual(errors, [])
            self.assertIn("app.py", files)
            self.assertEqual(files["app.py"]["old"], "x = 1\n")
            self.assertEqual(files["app.py"]["new"], "x = 2\n")

    def test_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = [("/abs/file.py", [], ["line"])]
            files, errors = r.build_proposal(root, {"sent": {}}, blocks)
            self.assertTrue(any("is an absolute path" in e for e in errors))

    def test_parent_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = [("../x.py", [], ["line"])]
            files, errors = r.build_proposal(root, {"sent": {}}, blocks)
            self.assertTrue(any("outside the project folder" in e for e in errors))

    def test_env_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocks = [(".env", [], ["KEY=val"])]
            files, errors = r.build_proposal(root, {"sent": {}}, blocks)
            self.assertTrue(any("protected or secret file" in e for e in errors))

    def test_existing_file_not_in_sent_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "unsent.py").write_text("val = 1\n", encoding="utf-8")
            blocks = [("unsent.py", ["val = 1"], ["val = 2"])]
            files, errors = r.build_proposal(root, {"sent": {}}, blocks)
            self.assertTrue(any("Gemini was never given this file" in e for e in errors))

    def test_empty_search_existing_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "exist.py").write_text("val = 1\n", encoding="utf-8")
            meta = {"sent": {"exist.py": "dummy"}}
            blocks = [("exist.py", [], ["val = 2"])]
            files, errors = r.build_proposal(root, meta, blocks)
            self.assertTrue(any("already exists, but the SEARCH part is empty" in e for e in errors))

    def test_unchanged_edit_dropped_from_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "same.py").write_text("same\n", encoding="utf-8")
            meta = {"sent": {"same.py": "dummy"}}
            blocks = [("same.py", ["same"], ["same"])]
            files, errors = r.build_proposal(root, meta, blocks)
            self.assertEqual(errors, [])
            self.assertEqual(files, {})


class TestIsSensitive(unittest.TestCase):
    def test_is_sensitive_cases(self):
        self.assertTrue(r.is_sensitive(Path(".env")))
        self.assertFalse(r.is_sensitive(Path(".env.example")))
        self.assertTrue(r.is_sensitive(Path("id_rsa")))
        self.assertTrue(r.is_sensitive(Path(".git/config")))
        self.assertFalse(r.is_sensitive(Path("src/app.py")))


class TestFindSecrets(unittest.TestCase):
    def test_detects_aws_key(self):
        fake = "AKIA" + "J" * 16
        findings = r.find_secrets(fake)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("AWS access key", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_detects_github_token(self):
        fake = "gh" + "p_" + "a" * 36
        findings = r.find_secrets(fake)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("GitHub token", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_detects_private_key_header(self):
        fake = "-----BEGIN " + "RSA PRIVATE KEY-----"
        findings = r.find_secrets(fake)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("private key", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_detects_password_in_url(self):
        fake = "secretpass" + "123"
        url = "https://user:" + fake + "@example.com"
        findings = r.find_secrets(url)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("password in a URL", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_detects_hardcoded_secret(self):
        fake = "secrettoken" + "123456789"
        line = 'api_key = "' + fake + '"'
        findings = r.find_secrets(line)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("hardcoded secret", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_detects_env_secret(self):
        fake = "envpassword" + "9"
        line = "DB_PASSWORD=" + fake
        findings = r.find_secrets(line)
        labels = [kind for _, kind, _ in findings]
        self.assertIn("secret in an environment assignment", labels)
        for _, _, masked in findings:
            self.assertNotIn(fake, masked)

    def test_ignores_placeholders(self):
        placeholders = [
            'password = "changeme123"',
            'api_key = "${API_KEY}"',
            'token = "<your-token>"',
            "postgres://user:password@localhost/db",
        ]
        for p in placeholders:
            with self.subTest(placeholder=p):
                self.assertEqual(r.find_secrets(p), [])


class BaseFilePatchedTestCase(unittest.TestCase):
    def setUp(self):
        self.orig_runs = r.RUNS
        self.orig_agent_file = r.AGENT_FILE
        self.orig_hooks_file = r.HOOKS_FILE
        self.orig_which = r.shutil.which

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        r.RUNS = self.temp_path / "runs"
        r.AGENT_FILE = self.temp_path / "agents" / f"{r.AGENT_NAME}.md"
        r.HOOKS_FILE = self.temp_path / "hooks.json"
        r.shutil.which = lambda cmd: "/usr/bin/agy"

    def tearDown(self):
        r.RUNS = self.orig_runs
        r.AGENT_FILE = self.orig_agent_file
        r.HOOKS_FILE = self.orig_hooks_file
        r.shutil.which = self.orig_which
        self.temp_dir.cleanup()


class TestSetupUninstall(BaseFilePatchedTestCase):
    def test_setup_creates_agent_and_hooks_and_problems_empty(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_setup(argparse.Namespace())

        self.assertTrue(r.AGENT_FILE.exists())
        self.assertEqual(r.AGENT_FILE.read_text(), r.AGENT_TEXT)
        self.assertTrue(r.HOOKS_FILE.exists())
        hooks = json.loads(r.HOOKS_FILE.read_text())
        self.assertIn(r.HOOK_KEY, hooks)
        self.assertEqual(hooks[r.HOOK_KEY], r.HOOK_ENTRY)
        self.assertEqual(r.setup_problems(), [])

    def test_setup_keeps_existing_hooks(self):
        r.HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.HOOKS_FILE.write_text(json.dumps({"other_hook": {"type": "command"}}))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_setup(argparse.Namespace())

        hooks = json.loads(r.HOOKS_FILE.read_text())
        self.assertIn("other_hook", hooks)
        self.assertIn(r.HOOK_KEY, hooks)

    def test_setup_invalid_json_hooks_exits_and_preserves_file(self):
        r.HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.HOOKS_FILE.write_text("{bad json")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                r.cmd_setup(argparse.Namespace())

        self.assertEqual(r.HOOKS_FILE.read_text(), "{bad json")
        self.assertFalse(r.AGENT_FILE.exists())

    def test_uninstall_removes_hook_key_keeps_other_entries(self):
        r.HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.HOOKS_FILE.write_text(json.dumps({r.HOOK_KEY: r.HOOK_ENTRY, "other_hook": {"type": "command"}}))
        r.AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.AGENT_FILE.write_text(r.AGENT_TEXT)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_uninstall(argparse.Namespace(purge=False))

        self.assertTrue(r.HOOKS_FILE.exists())
        hooks = json.loads(r.HOOKS_FILE.read_text())
        self.assertNotIn(r.HOOK_KEY, hooks)
        self.assertIn("other_hook", hooks)
        self.assertFalse(r.AGENT_FILE.exists())

    def test_uninstall_deletes_hooks_file_when_only_entry_and_deletes_agent(self):
        r.HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.HOOKS_FILE.write_text(json.dumps({r.HOOK_KEY: r.HOOK_ENTRY}))
        r.AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        r.AGENT_FILE.write_text(r.AGENT_TEXT)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_uninstall(argparse.Namespace(purge=False))

        self.assertFalse(r.HOOKS_FILE.exists())
        self.assertFalse(r.AGENT_FILE.exists())


class TestHookCommand(unittest.TestCase):
    def test_hook_command_disabled_without_guard(self):
        env = {k: v for k, v in os.environ.items() if k not in ("AGY_GEMINI_GUARD", "AGY_GEMINI_GUARD_LOG")}
        res = subprocess.run(
            ["sh", "-c", r.HOOK_COMMAND],
            input='{"toolCall":{"name":"view_file"}}',
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")

    def test_hook_command_blocks_with_guard(self):
        with tempfile.NamedTemporaryFile(mode="r+", delete=False) as f:
            log_path = Path(f.name)
        try:
            env = dict(os.environ, AGY_GEMINI_GUARD="1", AGY_GEMINI_GUARD_LOG=str(log_path))
            payload = '{"toolCall":{"name":"view_file"}}'
            res = subprocess.run(
                ["sh", "-c", r.HOOK_COMMAND],
                input=payload,
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(res.returncode, 0)
            data = json.loads(res.stdout.strip())
            self.assertEqual(data.get("decision"), "deny")
            log_content = log_path.read_text()
            self.assertIn(payload, log_content)
            self.assertIn("---TOOLCALL---", log_content)
        finally:
            if log_path.exists():
                log_path.unlink()


class TestApplyRevert(BaseFilePatchedTestCase):
    def _create_task(self, task_id="task-1", a_initial="x = 1\n"):
        root = self.temp_path / "project"
        root.mkdir(parents=True, exist_ok=True)
        if a_initial is not None:
            (root / "a.py").write_text(a_initial, encoding="utf-8")

        task_dir = r.RUNS / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "task_id": task_id,
            "root": str(root),
            "conversation_id": "test-conv",
            "sent": {},
            "rounds": [
                {
                    "round": 1,
                    "edit": True,
                    "proposal_ok": True,
                    "applied": False,
                    "reverted": False,
                }
            ],
        }
        (task_dir / "meta.json").write_text(json.dumps(meta))

        proposal = {
            "a.py": {"old": "x = 1\n", "new": "x = 2\n"},
            "b.py": {"old": None, "new": "y = 1\n"},
        }
        (task_dir / "round-1-proposal.json").write_text(json.dumps(proposal))
        return root, task_dir

    def test_apply_writes_both_files(self):
        root, _ = self._create_task("t1")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_apply(argparse.Namespace(task="t1"))

        self.assertEqual((root / "a.py").read_text(), "x = 2\n")
        self.assertEqual((root / "b.py").read_text(), "y = 1\n")

    def test_second_apply_exits(self):
        root, _ = self._create_task("t2")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_apply(argparse.Namespace(task="t2"))
            with self.assertRaises(SystemExit):
                r.cmd_apply(argparse.Namespace(task="t2"))

    def test_apply_exits_and_writes_nothing_when_file_changed(self):
        root, _ = self._create_task("t3", a_initial="x = 99\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                r.cmd_apply(argparse.Namespace(task="t3"))

        self.assertEqual((root / "a.py").read_text(), "x = 99\n")
        self.assertFalse((root / "b.py").exists())

    def test_revert_restores_a_and_deletes_b(self):
        root, _ = self._create_task("t4")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_apply(argparse.Namespace(task="t4"))
            r.cmd_revert(argparse.Namespace(task="t4"))

        self.assertEqual((root / "a.py").read_text(), "x = 1\n")
        self.assertFalse((root / "b.py").exists())

    def test_revert_skips_file_that_changed_after_apply(self):
        root, _ = self._create_task("t5")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r.cmd_apply(argparse.Namespace(task="t5"))
            (root / "a.py").write_text("x = 42\n", encoding="utf-8")
            r.cmd_revert(argparse.Namespace(task="t5"))

        self.assertEqual((root / "a.py").read_text(), "x = 42\n")
        self.assertFalse((root / "b.py").exists())


class TestCheckRoot(unittest.TestCase):
    def test_check_root_home_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                r.check_root(Path.home())

    def test_check_root_slash_exits(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                r.check_root(Path("/"))


if __name__ == "__main__":
    unittest.main()
