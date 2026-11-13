import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/update_fork_catalog.py"
SPEC = importlib.util.spec_from_file_location("catalog_update", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class CatalogTests(unittest.TestCase):
    def test_atomic_edits_preserve_ids_and_evidence(self):
        source = "# Catalog\n| F01 | old |\n[proof](https://github.com/x/y/commit/" + "a" * 40 + ")\n"
        self.assertIn("new", updater.apply_edits(source, {"edits": [{"old": "| F01 | old |", "new": "| F01 | new |"}]}))
        with self.assertRaises(ValueError):
            updater.apply_edits(source, {"edits": [{"old": "| F01 | old |", "new": "gone"}]} )
        with self.assertRaises(ValueError):
            updater.apply_edits(source, {"edits": [{"old": "missing", "new": "wrong"}]} )
        with self.assertRaises(ValueError):
            updater.apply_edits("duplicate duplicate", {"edits": [{"old": "duplicate", "new": "x"}]} )

    def test_rejects_loss_of_provenance_and_marker_injection(self):
        link = "https://github.com/x/y/commit/" + "a" * 40
        for old, new in [(link, "deleted"), ("old", "<!-- fork-catalog:last-reviewed=" + "b" * 40 + " -->")]:
            with self.subTest(new=new), self.assertRaises(ValueError):
                updater.apply_edits(old, {"edits": [{"old": old, "new": new}]})
        for invalid in [None, [], {"edits": "x"}, {"edits": [], "extra": True}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                updater.apply_edits("source", invalid)

    def test_quick_table_row_cannot_disappear_behind_repeated_id(self):
        source = "# Catalog\n## 一眼速查\n| F01 | old |\n## 追溯证据\n| F01 | proof |\n"
        with self.assertRaises(ValueError):
            updater.apply_edits(source, {"edits": [{"old": "| F01 | old |\n", "new": ""}]})
        valid = updater.apply_edits(source, {"edits": [{"old": "| F01 | old |", "new": "| F01 | renamed |"}]})
        self.assertIn("renamed", valid)

    def test_fragmented_checkpoint_injection_is_rejected(self):
        marker = "<!-- fork-catalog:last-reviewed=" + "a" * 40 + " -->"
        source = "# Catalog\nplaceholder\n" + marker + "\n"
        edits = [{"old": "placeholder", "new": "<!-- fork-catalog:last-revi"},
                 {"old": "last-revi\n", "new": "last-reviewed=" + "b" * 40 + " -->\n"}]
        with self.assertRaises(ValueError):
            updater.apply_edits(source, {"edits": edits})
        with self.assertRaises(ValueError):
            updater.set_checkpoint(source + marker, "c" * 40)
        self.assertEqual(updater.checkpoint(updater.set_checkpoint(source, "c" * 40)), "c" * 40)

    def test_model_catalog_redacts_checkpoint_identity(self):
        sha = "a" * 40
        source = "# Catalog\n| F01 | old |\n" + updater.set_checkpoint("", sha)
        model_catalog = updater.catalog_for_model(source)
        self.assertNotIn("last-reviewed", model_catalog)
        self.assertNotIn(sha, model_catalog)

    def test_response_requires_exact_model_and_complete_json(self):
        response = {"model": "deepseek-v4.1-flash", "choices": [{"finish_reason": "stop", "message": {"content": '{"edits": []}'}}]}
        self.assertEqual(updater.parse_response(response, "deepseek-v4.1-flash"), {"edits": []})
        for change in ["model", "finish", "json"]:
            bad = json.loads(json.dumps(response))
            if change == "model":
                bad["model"] = "other-model"
            if change == "finish":
                bad["choices"][0]["finish_reason"] = "length"
            if change == "json":
                bad["choices"][0]["message"]["content"] = 'prefix {"edits": []}'
            with self.subTest(change=change), self.assertRaises(ValueError):
                updater.parse_response(bad, "deepseek-v4.1-flash")

    def test_incomplete_model_response_is_retryable(self):
        response = {"model": "deepseek-v4.1-flash", "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with self.assertRaises(updater.IncompleteModelResponse):
            updater.parse_response(response, "deepseek-v4.1-flash")

    def test_model_edit_mismatch_is_retryable(self):
        with self.assertRaises(updater.ModelEditMismatch):
            updater.apply_edits("# Catalog\n", {"edits": [{"old": "missing", "new": "wrong"}]})

    def test_incomplete_batch_is_split_and_retried(self):
        source = "# Catalog\n| F01 | old |\n"
        responses = iter([
            updater.IncompleteModelResponse("model response was incomplete"),
            {"edits": [{"old": "| F01 | old |", "new": "| F01 | left |"}]},
            {"edits": []},
        ])
        with patch.object(updater, "evidence_for", return_value={"catalog": source}), patch.object(
            updater, "request_edits", side_effect=responses
        ) as request:
            reviewed, counts = updater.review_batch(Path("."), "unused", "http://localhost/v1", "deepseek-v4.1-flash", source, "a" * 40, "b" * 40, ["left.py", "right.py"])
        self.assertEqual(reviewed, "# Catalog\n| F01 | left |\n")
        self.assertEqual(counts, [1, 0])
        self.assertEqual(request.call_count, 3)

    def test_mismatched_batch_is_split_and_retried(self):
        source = "# Catalog\n| F01 | old |\n"
        responses = iter([
            {"edits": [{"old": "missing", "new": "wrong"}]},
            {"edits": [{"old": "| F01 | old |", "new": "| F01 | left |"}]},
            {"edits": []},
        ])
        with patch.object(updater, "evidence_for", return_value={"catalog": source}), patch.object(
            updater, "request_edits", side_effect=responses
        ) as request:
            reviewed, counts = updater.review_batch(Path("."), "unused", "http://localhost/v1", "deepseek-v4.1-flash", source, "a" * 40, "b" * 40, ["left.py", "right.py"])
        self.assertEqual(reviewed, "# Catalog\n| F01 | left |\n")
        self.assertEqual(counts, [1, 0])
        self.assertEqual(request.call_count, 3)

    def test_updater_checkpoint_recovers_missed_push_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            path = root / "FORK_FEATURES.md"
            path.write_text("# Catalog\n| F01 | old |\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            base = run("rev-parse", "HEAD")
            path.write_text(updater.set_checkpoint(path.read_text(), base))
            run("add", ".")
            run("commit", "-qm", "checkpoint")
            for i in range(2):
                (root / "feature.py").write_text(f"value = {i}\n")
                run("add", ".")
                run("commit", "-qm", f"feature {i}")
            before = path.read_text()
            with patch.object(updater, "request_edits", side_effect=RuntimeError("offline")):
                with self.assertRaises(RuntimeError):
                    updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash")
            self.assertEqual(path.read_text(), before)
            with patch.object(updater, "request_edits", return_value={"edits": [{"old": "| F01 | old |", "new": "| F01 | new |"}]}) as call:
                updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash")
                evidence = call.call_args.args[-1]
                self.assertIn("feature 0", evidence["commits"])
                self.assertIn("feature 1", evidence["commits"])
                self.assertNotIn("fork-catalog:last-reviewed", evidence["catalog"])
            head = run("rev-parse", "HEAD")
            self.assertEqual(updater.checkpoint(path.read_text()), head)
            run("add", ".")
            run("commit", "-qm", "catalog checkpoint")
            with patch.object(updater, "request_edits") as call:
                self.assertFalse(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
                call.assert_not_called()

    def test_large_change_batch_fails_without_model_call(self):
        with patch.object(updater, "git", side_effect=["a" * 40, "x" * 100_001]), patch.object(updater, "request_edits") as request:
            with self.assertRaises(ValueError):
                updater.evidence_for(Path("."), "catalog", "a" * 40, "b" * 40, ["feature.py"])
            request.assert_not_called()

    def test_large_change_is_split_into_bounded_path_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            (root / "baseline.txt").write_text("baseline\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            for name in ("first.txt", "second.txt"):
                (root / name).write_text("x" * 60_000)
            run("add", ".")
            run("commit", "-qm", "large change")
            head = run("rev-parse", "HEAD")
            base = run("rev-parse", "HEAD^")
            batches = updater.split_paths(root, base, head, ["first.txt", "second.txt"])
            self.assertEqual(batches, [["first.txt"], ["second.txt"]])

    def test_eval_result_dumps_stay_out_of_catalog_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            path = root / "FORK_FEATURES.md"
            path.write_text("# Catalog\n| F01 | old |\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            base = run("rev-parse", "HEAD")
            path.write_text(updater.set_checkpoint(path.read_text(), base))
            run("add", ".")
            run("commit", "-qm", "checkpoint")
            results = root / "evals" / "schema_dedupe" / "results" / "2026-11-09" / "cells.jsonl"
            results.parent.mkdir(parents=True)
            results.write_text('{"cell": "' + "x" * 300_000 + '"}\n')
            (root / "evals" / "schema_dedupe" / "worker.py").write_text("value = 1\n")
            run("add", ".")
            run("commit", "-qm", "eval run")
            with patch.object(updater, "request_edits", return_value={
                    "edits": [{"old": "| F01 | old |", "new": "| F01 | reviewed |"}]}) as request:
                self.assertTrue(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[-1]["changed_paths"], "evals/schema_dedupe/worker.py")

            results.write_text('{"cell": "' + "y" * 300_000 + '"}\n')
            run("add", ".")
            run("commit", "-qm", "another eval run")
            with patch.object(updater, "request_edits") as request:
                self.assertFalse(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
                request.assert_not_called()

    def test_desktop_bundles_stay_out_of_catalog_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            path = root / "FORK_FEATURES.md"
            path.write_text("# Catalog\n| F01 | old |\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            base = run("rev-parse", "HEAD")
            path.write_text(updater.set_checkpoint(path.read_text(), base))
            run("add", ".")
            run("commit", "-qm", "checkpoint")
            bundle = root / "plugins" / "demo" / "desktop" / "plugin.js"
            bundle.parent.mkdir(parents=True)
            bundle.write_text("var vendored='" + "x" * 300_000 + "'\n")
            (root / "plugins" / "demo" / "src" / "plugin.jsx").parent.mkdir(parents=True)
            (root / "plugins" / "demo" / "src" / "plugin.jsx").write_text("export const value = 1\n")
            run("add", ".")
            run("commit", "-qm", "rebuild bundle")
            with patch.object(updater, "request_edits", return_value={
                    "edits": [{"old": "| F01 | old |", "new": "| F01 | reviewed |"}]}) as request:
                self.assertTrue(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[-1]["changed_paths"], "plugins/demo/src/plugin.jsx")

            bundle.write_text("var vendored='" + "y" * 300_000 + "'\n")
            run("add", ".")
            run("commit", "-qm", "bundle only")
            with patch.object(updater, "request_edits") as request:
                self.assertFalse(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
                request.assert_not_called()

    def test_batched_update_is_atomic_when_a_later_batch_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            path = root / "FORK_FEATURES.md"
            path.write_text("# Catalog\n| F01 | old |\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            base = run("rev-parse", "HEAD")
            path.write_text(updater.set_checkpoint(path.read_text(), base))
            (root / "first.py").write_text("first = True\n")
            (root / "second.py").write_text("second = True\n")
            run("add", ".")
            run("commit", "-qm", "features")
            before = path.read_text()
            with patch.object(updater, "split_paths", return_value=[["first.py"], ["second.py"]]), patch.object(
                updater, "request_edits", side_effect=[{"edits": [{"old": "| F01 | old |", "new": "| F01 | first |"}]}, RuntimeError("offline")]
            ):
                with self.assertRaises(RuntimeError):
                    updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash")
            self.assertEqual(path.read_text(), before)

    def test_batched_update_applies_edits_sequentially_before_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = lambda *args: subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
            run("init", "-q", "-b", "dev")
            run("config", "user.name", "Test")
            run("config", "user.email", "test@example.invalid")
            path = root / "FORK_FEATURES.md"
            path.write_text("# Catalog\n| F01 | old |\n")
            run("add", ".")
            run("commit", "-qm", "baseline")
            base = run("rev-parse", "HEAD")
            path.write_text(updater.set_checkpoint(path.read_text(), base))
            (root / "first.py").write_text("first = True\n")
            (root / "second.py").write_text("second = True\n")
            run("add", ".")
            run("commit", "-qm", "features")
            with patch.object(updater, "split_paths", return_value=[["first.py"], ["second.py"]]), patch.object(
                updater, "request_edits", side_effect=[
                    {"edits": [{"old": "| F01 | old |", "new": "| F01 | first |"}]},
                    {"edits": [{"old": "| F01 | first |", "new": "| F01 | final |"}]},
                ]
            ) as request:
                self.assertTrue(updater.update(root, "unused", "http://localhost/v1", "deepseek-v4.1-flash"))
            self.assertIn("| F01 | final |", path.read_text())
            self.assertEqual(updater.checkpoint(path.read_text()), run("rev-parse", "HEAD"))
            self.assertEqual(request.call_count, 2)

    def test_large_evidence_is_explicitly_marked(self):
        value = updater.bounded("a" * 100, 40)
        self.assertIn("TRUNCATED", value)
        self.assertIn("100", value)

    def test_workflow_only_targets_dev_and_one_document(self):
        text = (SCRIPT.parents[2] / ".github/workflows/fork-catalog.yml").read_text()
        self.assertIn("branches: [dev]", text)
        self.assertIn("runs-on: [fork-catalog]", text)
        self.assertIn("timeout-minutes: 60", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("paths-ignore:", text)
        self.assertIn("sparse-checkout: |", text)
        self.assertIn("sparse-checkout-cone-mode: false", text)
        self.assertIn('git add -- FORK_FEATURES.md', text)
        self.assertNotIn("pull_request_target:", text)


if __name__ == "__main__":
    unittest.main()
