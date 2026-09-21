import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from context_store import (ContextError, ContextStore, MAX_FILE_BYTES, MAX_OUTPUT_CHARS, digest, encoded)




class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src/a.rs").write_text("first\nfn causal_signal() {}\nlast\n")
        self.policy = {"version": 1, "name": "test", "directories": [{"path": "src", "extensions": [".rs"]}]}
        self.store = ContextStore(self.root, self.policy)

    def tearDown(self):
        self.tmp.cleanup()

    def test_batch_search_and_exact_hashed_range(self):
        index = self.store.index()
        search = self.store.search(["causal_signal", "missing_symbol"], expected_view_id=index["view_id"])
        self.assertEqual(search["total_matches"], 1)
        match = search["matches"][0]
        read = self.store.read([{"path": match["path"], "sha256": match["sha256"], "start_line": 2, "end_line": 2}], index["view_id"])
        self.assertEqual(read["files"][0]["text"], "2: fn causal_signal() {}")
        self.assertEqual(read["files"][0]["sha256"], digest((self.root / "src/a.rs").read_bytes()))

    def test_changed_source_never_silently_reuses_old_version(self):
        index = self.store.index()
        known = {item["path"]: item["sha256"] for item in index["files"]}
        self.assertEqual(self.store.changes(known)["modified"], [])
        (self.root / "src/a.rs").write_text("new source\n")
        self.assertEqual(self.store.search(["new"], expected_view_id=index["view_id"])["status"], "changed_view")
        self.assertEqual(self.store.read([{"path": "src/a.rs", "sha256": known["src/a.rs"]}])["files"][0]["status"], "changed_file")
        change = self.store.changes(known)["modified"][0]
        self.assertEqual(change["status"], "modified")
        self.assertEqual(change["previous_sha256"], known["src/a.rs"])
        self.assertNotIn("text", change)

    def test_view_changes_when_a_file_is_replaced_with_same_size_and_mtime(self):
        index = self.store.index()
        path = self.root / "src/a.rs"
        before = path.stat()
        replacement = self.root / "replacement.tmp"
        data = bytearray(path.read_bytes())
        data[0] = ord("X")
        replacement.write_bytes(data)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        result = self.store.search(["causal_signal"], expected_view_id=index["view_id"])
        self.assertEqual(result["status"], "changed_view")

    def test_scope_and_traversal_denied(self):
        (self.root / "outside.md").write_text("private")
        for path in ("../outside.md", "/etc/passwd", "src/../outside.md", "outside.md", "src/.env", "src/a.pem", "src/credentials.rs"):
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.store.read([{"path": path}])

    def test_directory_search_accepts_final_slash_without_relaxing_boundary(self):
        (self.root / "src/nested").mkdir()
        (self.root / "src/nested/b.rs").write_text("fn causal_signal() {}\n")
        without_slash = self.store.search(["causal_signal"], "src")
        with_slash = self.store.search(["causal_signal"], "src/")
        self.assertEqual(with_slash["matches"], without_slash["matches"])
        self.assertEqual(with_slash["total_matches"], 2)
        for prefix in ("/", "../", "src/../", "src//", "src//nested/", "src/.state/"):
            with self.subTest(prefix=prefix), self.assertRaises(ContextError):
                self.store.search(["causal_signal"], prefix)

    def test_symlinks_hardlinks_and_fifos_are_not_read(self):
        (self.root / "private").mkdir()
        private = self.root / "private/hidden.rs"
        private.write_text("never expose this")
        (self.root / "src/link.rs").symlink_to(private)
        (self.root / "src/nested").symlink_to(self.root / "private", target_is_directory=True)
        os.link(private, self.root / "src/hard.rs")
        os.mkfifo(self.root / "src/pipe.rs")
        for path in ("src/link.rs", "src/nested/hidden.rs", "src/hard.rs", "src/pipe.rs"):
            result = self.store.read([{"path": path}])
            self.assertEqual(result["files"][0]["status"], "unavailable")
        self.assertEqual(self.store.search(["never expose"])["matches"], [])
        self.assertEqual(self.store.grep("never expose")["matches"], [])
        self.assertEqual([item["path"] for item in self.store.glob_files(["**/*.rs"])["files"]], ["src/a.rs"])

    def test_secret_content_is_blocked_and_large_files_are_readable(self):
        (self.root / "src/unsafe.rs").write_text("sk-" + "x" * 35)
        (self.root / "src/large.rs").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
        self.assertEqual(self.store.read([{"path": "src/unsafe.rs"}])["files"][0]["status"], "unavailable")
        self.assertEqual(self.store.search(["sk-"])["matches"], [])
        self.assertEqual(self.store.read_bytes("src/unsafe.rs", limit=1)["status"], "unavailable")
        self.assertNotIn("x" * 35, encoded(self.store.index()))
        large = self.store.read([{"path": "src/large.rs", "start_line": 1, "end_line": 1}])["files"][0]
        self.assertEqual(large["status"], "line_too_long")
        window = self.store.read_bytes("src/large.rs", offset=0, limit=8)
        self.assertEqual(window["text"], "a" * 8)
        self.assertEqual(window["bytes"], MAX_FILE_BYTES + 1)

    def test_python_search_fallback_blocks_a_secret_late_in_a_large_file(self):
        body = "needle\n" + "a" * (MAX_FILE_BYTES + 100) + "\nsk-" + "x" * 35 + "\n"
        (self.root / "src/large-late.rs").write_text(body)
        with mock.patch("context_store.RG", None):
            result = self.store.search(["needle"])
        self.assertEqual(result["matches"], [])
        blocked = next(item for item in result["skipped_files"] if item["path"] == "src/large-late.rs")
        self.assertIn("Credential-shaped", blocked["reason"])
        ranged = self.store.read([{"path": "src/large-late.rs", "start_line": 1, "end_line": 1}])
        self.assertEqual(ranged["files"][0]["status"], "unavailable")
        self.assertEqual(self.store.read_bytes("src/large-late.rs", limit=1)["status"], "unavailable")

    def test_large_file_line_and_byte_reads(self):
        body = "head\n" + "x" * 700_000 + "\ntail\n"
        (self.root / "src/large.rs").write_text(body)
        item = self.store.read([{"path": "src/large.rs", "start_line": 3, "end_line": 3}])["files"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["text"], "3: tail")
        self.assertEqual(item["bytes"], len(body.encode()))
        self.assertTrue(item["eof"])
        self.assertIsNone(item["next_line"])
        sha = digest((self.root / "src/large.rs").read_bytes())
        verified = self.store.read([{"path": "src/large.rs", "start_line": 3, "end_line": 3, "sha256": sha}])["files"][0]
        self.assertEqual(verified["status"], "ok")
        self.assertEqual(verified["sha256"], sha)
        stale = self.store.read([{"path": "src/large.rs", "start_line": 3, "end_line": 3, "sha256": "0" * 64}])["files"][0]
        self.assertEqual(stale["status"], "changed_file")
        window = self.store.read_bytes("src/large.rs", offset=0, limit=4)
        self.assertEqual(window["text"], "head")
        self.assertEqual(window["next_offset"], 4)
        self.assertFalse(window["eof"])
        tail = self.store.read_bytes("src/large.rs", offset=len(body.encode()) - 4, limit=64)
        self.assertTrue(tail["eof"])
        self.assertIsNone(tail["next_offset"])

    def test_search_pages_with_a_resumable_cursor(self):
        text = "".join(f"symbol_{i} " + "x" * 230 + "\n" for i in range(200))
        (self.root / "src/many.rs").write_text(text)
        found = []
        cursor = None
        pages = 0
        while True:
            result = self.store.search(["symbol_"], limit=80, cursor=cursor)
            self.assertLessEqual(len(encoded(result)), MAX_OUTPUT_CHARS)
            found.extend(item["line"] for item in result["matches"])
            cursor = result["next_cursor"]
            pages += 1
            self.assertLess(pages, 20)
            if cursor is None:
                break
        self.assertEqual(found, list(range(1, 201)))
        self.assertGreater(pages, 1)
        result = self.store.read([{"path": "src/many.rs"}] * 8)
        self.assertLessEqual(len(encoded(result)), MAX_OUTPUT_CHARS)
        self.assertTrue(result["deferred_paths"])
        self.assertGreater(result["files"][0]["next_line"], 1)

    def test_grep_regex_globs_context_and_output_modes(self):
        (self.root / "src/b.py").write_text("import os\nTODO: fix\n")
        policy = {"version": 1, "name": "test", "directories": [{"path": "src", "extensions": [".rs", ".py"]}]}
        store = ContextStore(self.root, policy)
        matches = store.grep(r"fn \w+_signal", include_globs=["**/*.rs"])
        self.assertEqual([item["path"] for item in matches["matches"]], ["src/a.rs"])
        self.assertEqual(matches["matches"][0]["line"], 2)
        self.assertEqual(len(matches["matches"][0]["sha256"]), 64)
        self.assertEqual(store.grep("signal", exclude_globs=["src/a.rs"])["total_matches"], 0)
        context = store.grep("TODO", before=1, after=1)["matches"][0]
        self.assertEqual(context["before"], [{"line": 1, "text": "import os"}])
        self.assertEqual(context["after"], [])
        self.assertEqual([item["path"] for item in store.grep("TODO", output_mode="files")["files"]], ["src/b.py"])
        counts = store.grep("TODO", output_mode="count")["counts"]
        self.assertEqual([(item["path"], item["count"]) for item in counts], [("src/b.py", 1)])
        self.assertEqual(store.grep("fn causal_signal", regex=False)["total_matches"], 1)
        self.assertEqual(store.grep("fn causal_signal", regex=False, case_sensitive=True)["total_matches"], 1)
        (self.root / "src/tail.rs").write_text("no trailing newline match_here")
        self.assertEqual([item["path"] for item in store.grep("match_here", output_mode="files")["files"]],
                         ["src/tail.rs"])
        self.assertEqual(store.grep("match_here")["matches"][0]["line"], 1)
        with mock.patch("context_store.RG", None):
            fallback = store.grep("match_here", output_mode="files")
        self.assertEqual([item["path"] for item in fallback["files"]], ["src/tail.rs"])
        with self.assertRaises(ContextError):
            store.grep("(")

    def test_glob_and_directory_listing_stay_inside_the_policy(self):
        (self.root / "src/nested").mkdir()
        (self.root / "src/nested/c.rs").write_text("fn nested() {}\n")
        (self.root / "outside").mkdir()
        (self.root / "outside/hidden.rs").write_text("fn hidden() {}\n")
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        globbed = self.store.glob_files(["**/*.rs"])
        self.assertEqual([item["path"] for item in globbed["files"]], ["src/a.rs", "src/nested/c.rs"])
        self.assertEqual(len(globbed["files"][0]["sha256"]), 64)
        self.assertEqual([item["path"] for item in self.store.glob_files(["*.rs"])["files"]],
                         ["src/a.rs", "src/nested/c.rs"])
        self.assertEqual([item["path"] for item in self.store.glob_files(["**/*.rs"], exclude=["src/nested/**"])["files"]],
                         ["src/a.rs"])
        listing = self.store.list_directory("", depth=3)
        self.assertEqual([entry["path"] for entry in listing["entries"]],
                         ["src", "src/a.rs", "src/nested", "src/nested/c.rs"])
        self.assertEqual([entry["path"] for entry in self.store.list_directory("", depth=2)["entries"]],
                         ["src", "src/a.rs", "src/nested"])
        self.assertNotIn("outside", encoded(listing))
        nested = [entry for entry in self.store.list_directory("src", depth=1)["entries"]
                  if entry["path"] == "src/nested"]
        self.assertEqual(nested[0]["type"], "directory")
        page = self.store.list_directory("", depth=3, limit=1)
        self.assertEqual(len(page["entries"]), 1)
        self.assertEqual(page["next_offset"], 1)
        with self.assertRaises(ContextError):
            self.store.list_directory("outside")

    def test_index_probes_only_the_returned_page(self):
        (self.root / "src/unsafe.rs").write_text("sk-" + "x" * 35)
        page = self.store.index(query="a.rs", limit=5)
        self.assertEqual([item["path"] for item in page["files"]], ["src/a.rs"])
        self.assertEqual(len(page["files"][0]["sha256"]), 64)
        self.assertEqual(page["unavailable_files"], 0)
        secret = self.store.index(query="unsafe.rs")
        self.assertEqual(secret["files"][0]["status"], "unavailable")
        self.assertNotIn("x" * 35, encoded(secret))

    def test_extension_allowlist_covers_common_languages_and_filenames(self):
        policy = {"version": 1, "name": "wide",
                  "directories": [{"path": ".", "extensions": [".go", ".java", ".yaml", ".sh"]}]}
        store = ContextStore(self.root, policy)
        for name in ("src/main.go", "src/App.java", "src/config.yaml", "src/run.sh", "Dockerfile", "Makefile"):
            (self.root / name).write_text("content\n")
        paths = [item["path"] for item in store.glob_files(
            ["**/*.go", "**/*.java", "**/*.yaml", "**/*.sh", "Dockerfile", "Makefile"])["files"]]
        self.assertEqual(paths, ["Dockerfile", "Makefile", "src/App.java", "src/config.yaml", "src/main.go", "src/run.sh"])
        with self.assertRaises(ContextError):
            ContextStore(self.root, {"version": 1, "directories": [{"path": "src", "extensions": [".key"]}]})

    def test_reads_do_not_require_a_workspace_view(self):
        result = self.store.read([{"path": "src/a.rs"}])
        self.assertIsNone(result["view_id"])
        self.assertEqual(result["files"][0]["status"], "ok")
        window = self.store.read_bytes("src/a.rs", limit=5)
        self.assertEqual(window["text"], "first")
        self.assertIsNone(window["view_id"])

    def test_removal_is_reported_as_deleted(self):
        index = self.store.index()
        known = {item["path"]: item["sha256"] for item in index["files"]}
        (self.root / "src/a.rs").unlink()
        result = self.store.changes(known)
        self.assertEqual([item["path"] for item in result["deleted"]], ["src/a.rs"])
        self.assertEqual(result["modified"], [])
        self.assertEqual(result["unchanged_files"], 0)

    def test_new_file_is_reported_as_added(self):
        index = self.store.index()
        known = {item["path"]: item["sha256"] for item in index["files"]}
        (self.root / "src/new.rs").write_text("unrelated new source\n")
        result = self.store.changes(known)
        self.assertEqual([item["path"] for item in result["added"]], ["src/new.rs"])
        self.assertEqual(len(result["added"][0]["sha256"]), 64)
        self.assertEqual(result["unchanged_files"], 1)
        self.assertEqual(result["total_changes"], 1)

    def test_change_pages_cover_added_modified_and_deleted_paths(self):
        (self.root / "src/old.rs").write_text("old\n")
        index = self.store.index()
        known = {item["path"]: item["sha256"] for item in index["files"]}
        (self.root / "src/a.rs").write_text("changed\n")
        (self.root / "src/old.rs").unlink()
        (self.root / "src/new.rs").write_text("new\n")
        found = {"added": [], "modified": [], "deleted": []}
        offset = 0
        pages = 0
        while True:
            page = self.store.changes(known, offset=offset, limit=1)
            for key in found:
                found[key].extend(item["path"] for item in page[key])
            pages += 1
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual(found, {"added": ["src/new.rs"], "modified": ["src/a.rs"],
                                 "deleted": ["src/old.rs"]})
        self.assertEqual(pages, 3)

    def test_write_file_creates_and_overwrites_inside_the_policy(self):
        created = self.store.write_file("src/new.rs", "fn added() {}\n")
        self.assertEqual(created["status"], "ok")
        self.assertTrue(created["created"])
        self.assertEqual(created["sha256"], digest(b"fn added() {}\n"))
        self.assertEqual((self.root / "src/new.rs").read_text(), "fn added() {}\n")
        rewritten = self.store.write_file("src/new.rs", "fn added() { 2 }\n",
                                          expected_sha256=created["sha256"], create=False)
        self.assertFalse(rewritten["created"])
        self.assertEqual(rewritten["previous_sha256"], created["sha256"])
        with self.assertRaises(ContextError):
            self.store.write_file("src/new.rs", "stale\n", expected_sha256=created["sha256"])
        with self.assertRaises(ContextError):
            self.store.write_file("src/missing.rs", "x\n", create=False)
        for path in ("../outside.rs", "src/.env", "src/key.pem", "outside.rs"):
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.store.write_file(path, "x\n")
        with self.assertRaises(ContextError):
            self.store.write_file("src/secret.rs", "sk-" + "x" * 35)

    def test_write_file_does_not_overwrite_a_file_created_during_the_create_race(self):
        original = self.store._commit

        def create_racer(path, data, expected_identity, create):
            (self.root / path).write_text("racer\n")
            return original(path, data, expected_identity, create)

        with mock.patch.object(self.store, "_commit", side_effect=create_racer):
            with self.assertRaises(ContextError):
                self.store.write_file("src/race.rs", "requested\n")
        self.assertEqual((self.root / "src/race.rs").read_text(), "racer\n")

    def test_existing_secret_content_cannot_be_overwritten_edited_or_deleted(self):
        path = self.root / "src/unsafe.rs"
        content = "prefix sk-" + "x" * 35 + "\n"
        path.write_text(content)
        with self.assertRaises(ContextError):
            self.store.write_file("src/unsafe.rs", "replacement\n")
        with self.assertRaises(ContextError):
            self.store.edit_file("src/unsafe.rs", [{"old": "prefix", "new": "other"}])
        with self.assertRaises(ContextError):
            self.store.delete_file("src/unsafe.rs")
        self.assertEqual(path.read_text(), content)

    def test_edit_file_applies_exact_replacements(self):
        before = digest((self.root / "src/a.rs").read_bytes())
        result = self.store.edit_file("src/a.rs", [{"old": "fn causal_signal() {}",
                                                  "new": "fn causal_signal() { 1 }"}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(result["previous_sha256"], before)
        self.assertEqual(result["sha256"], digest((self.root / "src/a.rs").read_bytes()))
        read = self.store.read([{"path": "src/a.rs", "start_line": 2, "end_line": 2}])
        self.assertEqual(read["files"][0]["text"], "2: fn causal_signal() { 1 }")
        with self.assertRaises(ContextError):
            self.store.edit_file("src/a.rs", [{"old": "missing text", "new": "x"}])
        (self.root / "src/dup.rs").write_text("same\nsame\n")
        with self.assertRaises(ContextError):
            self.store.edit_file("src/dup.rs", [{"old": "same", "new": "other"}])
        replaced = self.store.edit_file("src/dup.rs", [{"old": "same", "new": "other", "replace_all": True}])
        self.assertEqual(replaced["replacements"], 2)
        self.assertEqual((self.root / "src/dup.rs").read_text(), "other\nother\n")
        with self.assertRaises(ContextError):
            self.store.edit_file("src/dup.rs", [{"old": "other", "new": "x"}],
                                 expected_sha256="0" * 64)
        with self.assertRaises(ContextError):
            self.store.edit_file("src/dup.rs", [])

    def test_delete_file_removes_and_reports_the_last_hash(self):
        sha = digest((self.root / "src/a.rs").read_bytes())
        index = self.store.index()
        known = {item["path"]: item["sha256"] for item in index["files"]}
        result = self.store.delete_file("src/a.rs", expected_sha256=sha)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["previous_sha256"], sha)
        self.assertFalse((self.root / "src/a.rs").exists())
        self.assertEqual([item["path"] for item in self.store.changes(known)["deleted"]], ["src/a.rs"])
        with self.assertRaises(ContextError):
            self.store.delete_file("src/a.rs")
        (self.root / "src/b.rs").write_text("keep\n")
        with self.assertRaises(ContextError):
            self.store.delete_file("src/b.rs", expected_sha256="0" * 64)

    def test_writes_reject_symlinks_hardlinks_and_fifos(self):
        (self.root / "private").mkdir()
        private = self.root / "private/hidden.rs"
        private.write_text("never touch this")
        (self.root / "src/link.rs").symlink_to(private)
        os.link(self.root / "src/a.rs", self.root / "src/hard.rs")
        os.mkfifo(self.root / "src/pipe.rs")
        for path in ("src/link.rs", "src/hard.rs", "src/pipe.rs"):
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.store.write_file(path, "x\n")
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.store.edit_file(path, [{"old": "x", "new": "y"}])
            with self.subTest(path=path), self.assertRaises(ContextError):
                self.store.delete_file(path)
        self.assertEqual(private.read_text(), "never touch this")


if __name__ == "__main__":
    unittest.main()
