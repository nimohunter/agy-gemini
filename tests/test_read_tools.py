#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "agy-gemini" / "agy_run.py"
spec = importlib.util.spec_from_file_location("agy_run_rt", MODULE_PATH)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class FakeGemini:
    def __init__(self, responses):
        self.responses = list(responses)
        self.recorded_calls = []

    def __call__(self, task_dir, n, model, message, conversation_id, timeout):
        self.recorded_calls.append({
            "task_dir": task_dir,
            "n": n,
            "model": model,
            "message": message,
            "conversation_id": conversation_id,
            "timeout": timeout,
        })
        if self.responses:
            return self.responses.pop(0)
        return {"status": "SUCCESS", "response": "", "conversation_id": conversation_id}, 0


class TestReadTools(unittest.TestCase):
    def setUp(self):
        self.orig_call_gemini = r.call_gemini
        self.orig_read_max_bytes = r.READ_MAX_BYTES
        self.orig_search_max_matches = r.SEARCH_MAX_MATCHES
        self.orig_list_max = r.LIST_MAX
        self.orig_max_prompt_bytes = r.MAX_PROMPT_BYTES

    def tearDown(self):
        r.call_gemini = self.orig_call_gemini
        r.READ_MAX_BYTES = self.orig_read_max_bytes
        r.SEARCH_MAX_MATCHES = self.orig_search_max_matches
        r.LIST_MAX = self.orig_list_max
        r.MAX_PROMPT_BYTES = self.orig_max_prompt_bytes

    # 1. parse_requests
    def test_parse_requests_valid_list(self):
        text = "READ foo.py\nSEARCH def main\nLIST src"
        self.assertEqual(
            r.parse_requests(text),
            [("READ", "foo.py"), ("SEARCH", "def main"), ("LIST", "src")],
        )

    def test_parse_requests_read_range(self):
        text = "READ foo.py 10-25"
        self.assertEqual(r.parse_requests(text), [("READ", "foo.py 10-25")])

    def test_parse_requests_strips_markers_and_backticks(self):
        text = "- `READ foo.py`\n* `SEARCH pattern`"
        self.assertEqual(
            r.parse_requests(text),
            [("READ", "foo.py"), ("SEARCH", "pattern")],
        )

    def test_parse_requests_ignores_fences(self):
        text = "```\nREAD foo.py\n```"
        self.assertEqual(r.parse_requests(text), [("READ", "foo.py")])

    def test_parse_requests_caps_at_five(self):
        lines = [f"READ file{i}.py" for i in range(1, 8)]
        res = r.parse_requests("\n".join(lines))
        self.assertEqual(len(res), 5)
        self.assertEqual(res, [("READ", f"file{i}.py") for i in range(1, 6)])

    def test_parse_requests_non_request_line_returns_none(self):
        text = "READ foo.py\nHere is what I found."
        self.assertIsNone(r.parse_requests(text))

    def test_parse_requests_lowercase_returns_none(self):
        self.assertIsNone(r.parse_requests("read a.py"))

    def test_parse_requests_empty_returns_none(self):
        self.assertIsNone(r.parse_requests(""))
        self.assertIsNone(r.parse_requests("   \n   "))

    # 2. project_files without git
    def test_project_files_without_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "mod.js").write_text("console.log(1);")
            (root / "target").mkdir()
            (root / "target" / "out.bin").write_text("binary")
            (root / ".env").write_text("SECRET=1")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print(1)")
            (root / "readme.txt").write_text("hello")

            files = r.project_files(root)
            self.assertIn("readme.txt", files)
            self.assertIn("src/app.py", files)
            self.assertNotIn("node_modules/mod.js", files)
            self.assertNotIn("target/out.bin", files)
            self.assertNotIn(".env", files)
            self.assertTrue(all("/" in p or "\\" not in p for p in files))

    # 3. project_files with git
    def test_project_files_with_git(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text("ignored.txt\n")
            (root / "ignored.txt").write_text("ignore me")
            (root / "kept.txt").write_text("keep me")

            files = r.project_files(root)
            self.assertEqual(files, [".gitignore", "kept.txt"])

    # 4. run_request READ
    def test_run_request_read_whole_file_has_line_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "file.txt").write_text("first line\nsecond line\n")
            text, entry = r.run_request(root, "READ", "file.txt", False)
            self.assertTrue(text.startswith("=== READ file.txt\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("   1| first line", text)
            self.assertIn("   2| second line", text)

    def test_run_request_read_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "multi.txt").write_text("line 1\nline 2\nline 3\nline 4\n")
            text, entry = r.run_request(root, "READ", "multi.txt 2-3", False)
            self.assertTrue(text.startswith("=== READ multi.txt 2-3\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("   2| line 2", text)
            self.assertIn("   3| line 3", text)
            self.assertNotIn("line 1", text)
            self.assertNotIn("line 4", text)

    def test_run_request_read_secret_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_key = "AKIA" + "Q" * 16
            (root / "secret.txt").write_text(f"line 1\n{fake_key}\nline 3\n")
            text, entry = r.run_request(root, "READ", "secret.txt", False)
            self.assertTrue(text.startswith("=== READ secret.txt\n"))
            self.assertEqual(entry["status"], "refused")
            self.assertEqual(entry["note"], "possible secrets")
            self.assertNotIn(fake_key, text)
            self.assertNotIn("line 1", text)
            self.assertNotIn("line 3", text)

    def test_run_request_read_env_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("FOO=bar\n")
            text, entry = r.run_request(root, "READ", ".env", False)
            self.assertTrue(text.startswith("=== READ .env\n"))
            self.assertEqual(entry["status"], "refused")
            self.assertEqual(entry["note"], "protected file")

    def test_run_request_read_parent_traversal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text, entry = r.run_request(root, "READ", "../x", False)
            self.assertTrue(text.startswith("=== READ ../x\n"))
            self.assertEqual(entry["status"], "refused")
            self.assertEqual(entry["note"], "outside project folder")

    def test_run_request_read_absolute_path_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            abs_path = str(Path("/tmp/foo.txt").resolve())
            text, entry = r.run_request(root, "READ", abs_path, False)
            self.assertTrue(text.startswith(f"=== READ {abs_path}\n"))
            self.assertEqual(entry["status"], "refused")
            self.assertEqual(entry["note"], "absolute path")

    def test_run_request_read_skip_dir_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "foo.js").write_text("console.log(1);")
            text, entry = r.run_request(root, "READ", "node_modules/foo.js", False)
            self.assertTrue(text.startswith("=== READ node_modules/foo.js\n"))
            self.assertEqual(entry["status"], "refused")
            self.assertEqual(entry["note"], "ignored directory")

    def test_run_request_read_binary_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin.dat").write_bytes(b"\0binary")
            text, entry = r.run_request(root, "READ", "bin.dat", False)
            self.assertTrue(text.startswith("=== READ bin.dat\n"))
            self.assertEqual(entry["status"], "error")

    def test_run_request_read_max_bytes_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r.READ_MAX_BYTES = 50
            (root / "long.txt").write_text("\n".join(f"line {i}" for i in range(25)))
            text, entry = r.run_request(root, "READ", "long.txt", False)
            self.assertTrue(text.startswith("=== READ long.txt\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("[cut: file continues; ask for a line range]", text)

    # 5. run_request SEARCH
    def test_run_request_search_match_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test.txt").write_text("alpha\nbeta target\ngamma\n")
            text, entry = r.run_request(root, "SEARCH", "target", False)
            self.assertTrue(text.startswith("=== SEARCH target\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("test.txt:2: beta target", text)

    def test_run_request_search_hides_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_token = "ghp_" + "A" * 36
            (root / "auth.py").write_text(f"token = '{fake_token}'\n")
            text, entry = r.run_request(root, "SEARCH", "token", False)
            self.assertTrue(text.startswith("=== SEARCH token\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("auth.py:1: [hidden: possible secret]", text)
            self.assertNotIn(fake_token, text)

    def test_run_request_search_invalid_regex_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text, entry = r.run_request(root, "SEARCH", "[invalid(", False)
            self.assertTrue(text.startswith("=== SEARCH [invalid(\n"))
            self.assertEqual(entry["status"], "error")

    def test_run_request_search_max_matches_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r.SEARCH_MAX_MATCHES = 2
            (root / "matches.txt").write_text("\n".join(f"FINDME {i}" for i in range(5)))
            text, entry = r.run_request(root, "SEARCH", "FINDME", False)
            self.assertTrue(text.startswith("=== SEARCH FINDME\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("Total matches: 5 (cut to 2)", text)
            match_lines = [l for l in text.splitlines() if l.startswith("matches.txt:")]
            self.assertEqual(len(match_lines), 2)

    # 6. run_request LIST
    def test_run_request_list_dot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("b")
            text, entry = r.run_request(root, "LIST", ".", False)
            self.assertTrue(text.startswith("=== LIST .\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("a.txt", text)
            self.assertIn("sub/b.txt", text)

    def test_run_request_list_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a")
            (root / "sub").mkdir()
            (root / "sub" / "b.txt").write_text("b")
            text, entry = r.run_request(root, "LIST", "sub", False)
            self.assertTrue(text.startswith("=== LIST sub\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("sub/b.txt", text)
            self.assertNotIn("a.txt", text)

    def test_run_request_list_max_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r.LIST_MAX = 1
            (root / "f1.txt").write_text("1")
            (root / "f2.txt").write_text("2")
            text, entry = r.run_request(root, "LIST", ".", False)
            self.assertTrue(text.startswith("=== LIST .\n"))
            self.assertEqual(entry["status"], "ok")
            self.assertIn("Total: 2 files (cut to 1)", text)
            file_entries = [l for l in text.splitlines()[1:] if not l.startswith("Total:")]
            self.assertEqual(len(file_entries), 1)

    def test_run_request_list_file_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a")
            text, entry = r.run_request(root, "LIST", "a.txt", False)
            self.assertTrue(text.startswith("=== LIST a.txt\n"))
            self.assertEqual(entry["status"], "error")
            self.assertEqual(entry["note"], "'a.txt' is not a directory")

    # 7. read_loop
    def test_read_loop_read_request_success(self):
        with tempfile.TemporaryDirectory() as task_tmp, tempfile.TemporaryDirectory() as root_tmp:
            task_dir = Path(task_tmp)
            root = Path(root_tmp)
            (root / "a.py").write_text("print('hello')\n")
            meta = {"sent": {}}
            a = argparse.Namespace(timeout=480, max_reads=8, allow_secrets=False)

            fake = FakeGemini([
                ({"status": "SUCCESS", "response": "READ a.py", "conversation_id": "conv-1",
                  "usage": {"input_tokens": 10, "output_tokens": 5}}, 1),
                ({"status": "SUCCESS", "response": "FINAL ANSWER", "conversation_id": "conv-1",
                  "usage": {"input_tokens": 20, "output_tokens": 15}}, 0),
            ])
            r.call_gemini = fake

            res, tool_calls, read_log = r.read_loop(task_dir, 1, "test-model", "start", None, a, root, meta)

            self.assertEqual(res["response"], "FINAL ANSWER")
            self.assertEqual(len(read_log), 1)
            self.assertEqual(read_log[0]["status"], "ok")
            self.assertIn("a.py", meta["sent"])
            self.assertEqual(meta["sent"]["a.py"], r.sha((root / "a.py").read_text()))
            self.assertEqual(len(fake.recorded_calls), 2)
            self.assertIn("=== READ a.py", fake.recorded_calls[1]["message"])
            self.assertEqual(fake.recorded_calls[1]["conversation_id"], "conv-1")
            self.assertEqual(res["usage"]["input_tokens"], 30)
            self.assertEqual(res["usage"]["output_tokens"], 20)

            log_file = task_dir / "round-1-reads.log"
            self.assertTrue(log_file.exists())
            log_lines = log_file.read_text().splitlines()
            self.assertEqual(len(log_lines), 1)
            self.assertEqual(json.loads(log_lines[0])["target"], "a.py")

    def test_read_loop_max_reads_cutoff_and_read_limit(self):
        with tempfile.TemporaryDirectory() as task_tmp, tempfile.TemporaryDirectory() as root_tmp:
            task_dir = Path(task_tmp)
            root = Path(root_tmp)
            meta = {"sent": {}}
            a = argparse.Namespace(timeout=480, max_reads=1, allow_secrets=False)

            fake = FakeGemini([
                ({"status": "SUCCESS", "response": "LIST .", "conversation_id": "conv-1"}, 0),
                ({"status": "SUCCESS", "response": "LIST .", "conversation_id": "conv-1"}, 0),
                ({"status": "SUCCESS", "response": "LIST .", "conversation_id": "conv-1"}, 0),
            ])
            r.call_gemini = fake

            res, tool_calls, read_log = r.read_loop(task_dir, 1, "test-model", "start", None, a, root, meta)

            self.assertEqual(len(fake.recorded_calls), 3)
            self.assertIn("=== LIST .", fake.recorded_calls[1]["message"])
            self.assertEqual(
                fake.recorded_calls[2]["message"],
                "No more requests are allowed. Give your final answer now, in the output format above.",
            )
            self.assertEqual(res["status"], "READ_LIMIT")

    def test_read_loop_stops_on_error(self):
        with tempfile.TemporaryDirectory() as task_tmp, tempfile.TemporaryDirectory() as root_tmp:
            task_dir = Path(task_tmp)
            root = Path(root_tmp)
            meta = {"sent": {}}
            a = argparse.Namespace(timeout=480, max_reads=8, allow_secrets=False)

            fake = FakeGemini([
                ({"status": "ERROR", "response": "", "error": "agy error"}, 0),
            ])
            r.call_gemini = fake

            res, tool_calls, read_log = r.read_loop(task_dir, 1, "test-model", "start", None, a, root, meta)
            self.assertEqual(len(fake.recorded_calls), 1)
            self.assertEqual(res["status"], "ERROR")

    def test_read_loop_prompt_bytes_cut(self):
        with tempfile.TemporaryDirectory() as task_tmp, tempfile.TemporaryDirectory() as root_tmp:
            task_dir = Path(task_tmp)
            root = Path(root_tmp)
            r.MAX_PROMPT_BYTES = 200
            (root / "large.txt").write_text("\n".join(f"line content {i}" for i in range(20)))
            meta = {"sent": {}}
            a = argparse.Namespace(timeout=480, max_reads=8, allow_secrets=False)

            fake = FakeGemini([
                ({"status": "SUCCESS", "response": "READ large.txt", "conversation_id": "conv-1"}, 0),
                ({"status": "SUCCESS", "response": "FINAL ANSWER", "conversation_id": "conv-1"}, 0),
            ])
            r.call_gemini = fake

            res, tool_calls, read_log = r.read_loop(task_dir, 1, "test-model", "start", None, a, root, meta)
            self.assertEqual(len(fake.recorded_calls), 2)
            second_msg = fake.recorded_calls[1]["message"]
            self.assertTrue(second_msg.endswith("[cut: results too large; ask for less]"))

    # 8. CLI options
    def test_ask_help_shows_read_tools_options(self):
        res = subprocess.run(
            [sys.executable, str(MODULE_PATH), "ask", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--read-tools", res.stdout)
        self.assertIn("--max-reads", res.stdout)


if __name__ == "__main__":
    unittest.main()
