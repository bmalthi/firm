import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("loc", Path(__file__).resolve().parents[1] / "loc.py")
loc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loc)


class LineCounts(unittest.TestCase):
    def test_comments_strings_docstrings_and_blanks(self):
        source = "\n".join(
            [
                '"""Module docs.',
                "More docs.",
                '"""',
                "",
                "# a comment",
                'value = "# not a comment"  # inline',
                'text = """data',
                "# still data",
                '"""',
                "",
                "def example():",
                '    """Function docs."""',
                "    return value",
                "",
            ]
        )
        self.assertEqual(
            loc.count_lines(source),
            {
                "files": 1,
                "code": 6,
                "comments": 6,
                "blank": 2,
                "lines": 13,
            },
        )

    def test_unicode_inline_docstring_and_parenthesized_docstring(self):
        source = 'def café(): "docs"; return "#"  # comment\n'
        self.assertEqual(loc.count_lines(source)["code"], 1)
        self.assertEqual(loc.count_lines(source)["comments"], 1)
        source = '(\n    "one"\n    "two"\n)\n'
        self.assertEqual(loc.count_lines(source)["code"], 0)
        self.assertEqual(loc.count_lines(source)["comments"], 4)

    def test_blank_lines_inside_strings_and_no_final_newline(self):
        counts = loc.count_lines('"""docs\n\nmore"""\nvalue = """data\n\nmore"""')
        self.assertEqual(counts, {"files": 1, "code": 2, "comments": 2, "blank": 2, "lines": 6})
        self.assertEqual(loc.count_lines("")["lines"], 0)
        self.assertEqual(loc.count_lines("# only")["comments"], 1)

    def test_class_and_async_docstrings(self):
        source = (
            'class Example:\n    "docs"\n    async def run(self):\n        "docs"\n        pass\n'
        )
        self.assertEqual(loc.count_lines(source)["comments"], 2)
        self.assertEqual(loc.count_lines(source)["code"], 3)

    def test_physical_line_endings_and_string_separator_characters(self):
        source = 'value = "a\x85b"\r\n\r\n# comment\r\n'
        self.assertEqual(
            loc.count_lines(source),
            {
                "files": 1,
                "code": 1,
                "comments": 1,
                "blank": 1,
                "lines": 3,
            },
        )


class ScanAndSnapshots(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def test_discovery_exclusions_test_helpers_and_symlinks(self):
        source = self.write("src/app.py", "answer = 42\n")
        self.write("tests/helpers.py", "# fixture\nanswer = 42\n")
        self.write("conftest.py", "pass\n")
        self.write("src/example_test.py", "pass\n")
        self.write("specs/example.py", "pass\n")
        self.write("src/types.pyi", "answer: int\n")
        self.write(".venv/ignored.py", "invalid python!")
        self.write("generated/ignored.py", "invalid python!")
        self.write("readme.md", "not python")
        (self.root / "linked.py").symlink_to(source)
        (self.root / "linked_dir").symlink_to(self.root / "src", target_is_directory=True)
        result = loc.report(self.root, ["generated/*"], ["specs/*"])
        self.assertEqual(result["groups"]["source"]["code"], 2)
        self.assertEqual(result["groups"]["tests"]["code"], 4)
        self.assertEqual(result["groups"]["total"]["files"], 6)
        tests_only = loc.report(self.root / "tests", [], [])
        self.assertEqual(tests_only["groups"]["tests"]["code"], 1)
        self.assertEqual(tests_only["groups"]["source"]["files"], 0)

    def test_nested_gitignore_and_untracked_files(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "ignored.py\n")
        self.write("nested/.gitignore", "*.py\n!kept.py\n")
        self.write("ignored.py", "invalid python!")
        self.write("nested/ignored.py", "invalid python!")
        self.write("nested/kept.py", "pass\n")
        self.write("new file.py", "pass\n")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "ignored.py"], check=True)
        paths = [entry["path"] for entry in loc.report(self.root, [], [])["files"]]
        self.assertEqual(paths, ["nested/kept.py", "new file.py"])
        nested = loc.report(self.root / "nested", [], [])
        self.assertEqual([entry["path"] for entry in nested["files"]], ["kept.py"])

    def test_encoded_source(self):
        path = self.root / "encoded.py"
        path.write_bytes(b'# coding: latin-1\nname = "caf\xe9"\n')
        result = loc.report(self.root, [], [])
        self.assertEqual(result["groups"]["total"]["code"], 1)
        self.assertEqual(result["groups"]["total"]["comments"], 1)

    def test_cli_save_compare_and_update(self):
        self.write("app.py", "pass\n")
        snapshot = self.root / "snapshot.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(loc.main([str(self.root), "--save", str(snapshot)]), 0)
        self.write("tests/test_app.py", "# check\nassert True\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            loc.main(
                [str(self.root), "--compare", str(snapshot), "--save", str(snapshot), "--json"]
            )
        result = json.loads(output.getvalue())
        self.assertEqual(result["delta"]["tests"]["code"], 1)
        self.assertEqual(result["delta"]["total"]["comments"], 1)
        self.assertEqual(json.loads(snapshot.read_text()), result)
        (self.root / "app.py").unlink()
        current = loc.report(self.root, [], [])
        self.assertEqual(loc.compare(current, result)["source"]["code"], -1)
        current["options"]["exclude"] = ["tools/*"]
        with self.assertRaisesRegex(ValueError, "options differs"):
            loc.compare(current, result)

    def test_invalid_source_does_not_replace_snapshot(self):
        self.write("broken.py", '"""unfinished')
        snapshot = self.write("snapshot.json", "keep me")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            loc.main([str(self.root), "--save", str(snapshot)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(snapshot.read_text(), "keep me")


class DailyHistory(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)

    def commit(self, date, files):
        for name, source in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        stamp = f"{date}T12:00:00+0000"
        environment = {
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "PATH": os.environ["PATH"],
        }
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-q", "-m", date],
            check=True,
            env=environment,
        )

    def test_last_commit_of_each_day_and_subdirectory_scans(self):
        self.commit("2024-01-01", {"app.py": "pass\n"})
        self.commit("2024-01-02", {"pkg/app.py": "# note\nvalue = 1\n"})
        self.commit("2024-01-02", {"tests/test_app.py": "assert True\n"})
        (self.root / "untracked.py").write_text("invalid python!", encoding="utf-8")
        days = loc.history(self.root, [], [], None)
        self.assertEqual([day["date"] for day in days], ["2024-01-01", "2024-01-02"])
        self.assertEqual(days[0]["groups"]["total"], loc.count_lines("pass\n"))
        self.assertEqual(days[1]["groups"]["source"]["code"], 2)
        self.assertEqual(days[1]["groups"]["source"]["comments"], 1)
        self.assertEqual(days[1]["groups"]["tests"]["code"], 1)
        self.assertEqual(days[1]["groups"]["total"]["files"], 3)
        nested = loc.history(self.root / "pkg", [], [], None)
        self.assertEqual([day["groups"]["total"]["files"] for day in nested], [0, 1])
        excluded = loc.history(self.root, ["pkg/*"], [], None)
        self.assertEqual(excluded[1]["groups"]["total"]["files"], 2)
        since = loc.history(self.root, [], [], "2024-01-02T00:00:00+0000")
        self.assertEqual([day["date"] for day in since], ["2024-01-02"])

    def test_cli_daily_output_and_missing_repository(self):
        self.commit("2024-01-01", {"app.py": "pass\n"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            loc.main([str(self.root), "--daily", "--json"])
        daily = json.loads(output.getvalue())["daily"]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["date"], "2024-01-01")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            loc.main([str(self.root), "--daily"])
        self.assertIn("2024-01-01", output.getvalue())
        self.assertIn("Daily totals", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            loc.main([str(self.root), "--since", "2024-01-01"])
        self.assertEqual(error.exception.code, 2)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        plain = Path(outside.name)
        with contextlib.redirect_stderr(io.StringIO()) as problem, self.assertRaises(SystemExit):
            loc.main([str(plain), "--daily"])
        self.assertIn("not a git repository", problem.getvalue())


if __name__ == "__main__":
    unittest.main()
