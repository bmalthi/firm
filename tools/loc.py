"""Count Python source, comment, and test lines without importing project code."""

import argparse
import ast
import fnmatch
import io
import json
import os
import subprocess
import tokenize
from datetime import UTC, datetime
from pathlib import Path

METRICS = ("files", "code", "comments", "blank", "lines")
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".cache",
    ".state",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    "node_modules",
    "site-packages",
    "build",
    "dist",
    "vendor",
}
IGNORED_TOKENS = {
    tokenize.ENCODING,
    tokenize.ENDMARKER,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.NEWLINE,
    tokenize.NL,
}


def count_lines(source: str, filename: str = "<source>") -> dict[str, int]:
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    tree = ast.parse(source, filename=filename)
    lines = source.split("\n") if source else []
    if lines and not lines[-1]:
        lines.pop()
    nonblank = {number for number, line in enumerate(lines, 1) if line.strip()}
    docstrings = []

    def position(line: int, byte_column: int) -> tuple[int, int]:
        # AST columns use UTF-8 bytes; tokenizer columns use characters.
        column = len(lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8"))
        return line, column

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.append(
                (
                    position(first.lineno, first.col_offset),
                    position(first.end_lineno, first.end_col_offset),
                )
            )

    code, comments = set(), set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in IGNORED_TOKENS:
            continue
        end_line = token.end[0] - (token.end[1] == 0)
        occupied = set(range(token.start[0], end_line + 1)) & nonblank
        is_docstring = any(start <= token.start and token.end <= end for start, end in docstrings)
        if token.type == tokenize.COMMENT or is_docstring:
            comments.update(occupied)
        else:
            code.update(occupied)
    # Continuation-only lines (a backslash) have no significant token.
    code.update(nonblank - comments)
    return {
        "files": 1,
        "code": len(code),
        "comments": len(comments),
        "blank": len(lines) - len(nonblank),
        "lines": len(lines),
    }


def decode_source(data: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def excluded_path(relative: Path, excludes: list[str]) -> bool:
    return any(
        part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in relative.parts
    ) or any(fnmatch.fnmatchcase(relative.as_posix(), pattern) for pattern in excludes)


def source_files(root: Path, excludes: list[str]) -> list[Path]:
    def excluded(path: Path) -> bool:
        return path.is_symlink() or excluded_path(path.relative_to(root), excludes)

    def walk_error(error: OSError) -> None:
        raise error

    candidates = []
    for directory, dirs, files in os.walk(root, onerror=walk_error):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if not excluded(parent / name)]
        candidates.extend(
            parent / name
            for name in files
            if Path(name).suffix in {".py", ".pyi"} and not excluded(parent / name)
        )
    if not candidates:
        return []

    # Git handles nested .gitignore rules and includes untracked, non-ignored files.
    # --no-index also excludes ignored files that are already tracked.
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return sorted(candidates)
    if result.returncode:
        return sorted(candidates)
    output = run_git(
        root,
        ["check-ignore", "--no-index", "-z", "--stdin"],
        stdin=b"".join(os.fsencode(path.relative_to(root)) + b"\0" for path in candidates),
        allowed=(0, 1),
    )
    ignored = {os.fsdecode(path) for path in output.split(b"\0") if path}
    return sorted(path for path in candidates if str(path.relative_to(root)) not in ignored)


def is_test(path: Path, extra_patterns: list[str]) -> bool:
    return (
        bool({"test", "tests"} & set(path.parts[:-1]))
        or path.name == "conftest.py"
        or path.stem.startswith("test_")
        or path.stem.endswith("_test")
        or any(fnmatch.fnmatchcase(path.as_posix(), pattern) for pattern in extra_patterns)
    )


def group_counts(
    root: Path, test_patterns: list[str], counted: list[tuple[Path, dict[str, int]]]
) -> tuple[dict, list]:
    groups = {group: dict.fromkeys(METRICS, 0) for group in ("source", "tests", "total")}
    files = []
    for relative, counts in counted:
        testing = root.name in {"test", "tests"} or is_test(relative, test_patterns)
        group = "tests" if testing else "source"
        files.append({"path": relative.as_posix(), "group": group, **counts})
        for metric in METRICS:
            groups[group][metric] += counts[metric]
            groups["total"][metric] += counts[metric]
    return groups, files


def report(root: Path, excludes: list[str], test_patterns: list[str]) -> dict:
    counted = []
    for path in source_files(root, excludes):
        with tokenize.open(path) as stream:
            counted.append((path.relative_to(root), count_lines(stream.read(), str(path))))
    groups, files = group_counts(root, test_patterns, counted)
    return {
        "root": str(root),
        "created_at": datetime.now(UTC).isoformat(),
        "options": {"exclude": sorted(excludes), "test_pattern": sorted(test_patterns)},
        "groups": groups,
        "files": files,
    }


def run_git(
    root: Path, arguments: list[str], stdin: bytes | None = None, allowed: tuple[int, ...] = (0,)
) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], input=stdin, capture_output=True, check=False
    )
    if result.returncode not in allowed:
        raise ValueError(f"git {arguments[0]} failed: {os.fsdecode(result.stderr).strip()}")
    return result.stdout


def daily_commits(root: Path, since: str | None) -> list[tuple[str, str]]:
    """Return the last commit of each day reachable from HEAD, oldest day first."""
    arguments = ["log", "--date-order", "--date=format:%Y-%m-%d", "--pretty=%cd %H"]
    if since:
        arguments.append(f"--since={since}")
    latest: dict[str, str] = {}
    for line in os.fsdecode(run_git(root, arguments)).splitlines():
        date, _, commit = line.partition(" ")
        # git log lists the newest commit of a day first.
        latest.setdefault(date, commit)
    return sorted(latest.items())


def commit_files(
    root: Path, commit: str, prefix: str, excludes: list[str]
) -> list[tuple[str, Path]]:
    """Return (blob, path) for the Python files tracked under prefix at a commit."""
    # --full-tree keeps paths relative to the scanned directory rather than the cwd.
    # A missing tree means the scanned directory did not exist yet at that commit.
    arguments = ["ls-tree", "-r", "-z", "--full-tree", f"{commit}:{prefix}"]
    output = run_git(root, arguments, allowed=(0, 128))
    entries = []
    for entry in os.fsdecode(output).split("\0"):
        if not entry:
            continue
        attributes, _, name = entry.partition("\t")
        mode, kind, blob = attributes.split(" ", 2)
        relative = Path(name)
        if kind != "blob" or mode == "120000" or relative.suffix not in {".py", ".pyi"}:
            continue
        if not excluded_path(relative, excludes):
            entries.append((blob, relative))
    return sorted(entries, key=lambda entry: entry[1])


def read_blobs(root: Path, wanted: dict[str, Path], commit: str) -> dict[str, dict[str, int]]:
    if not wanted:
        return {}
    stream = io.BytesIO(
        run_git(
            root, ["cat-file", "--batch"], stdin="".join(f"{blob}\n" for blob in wanted).encode()
        )
    )
    counts = {}
    for blob, relative in wanted.items():
        header = stream.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise ValueError(f"git cat-file returned no content for {blob}")
        data = stream.read(int(header[2]))
        stream.read(1)
        try:
            counts[blob] = count_lines(decode_source(data), str(relative))
        except (UnicodeError, SyntaxError, tokenize.TokenError, ValueError) as error:
            raise ValueError(f"{commit[:10]}:{relative.as_posix()}: {error}") from error
    return counts


def history(root: Path, excludes: list[str], test_patterns: list[str], since: str | None) -> list:
    """Count the tree at the last commit of each day, newest day last."""
    prefix = os.fsdecode(run_git(root, ["rev-parse", "--show-prefix"])).strip().rstrip("/")
    counted: dict[str, dict[str, int]] = {}
    days = []
    for date, commit in daily_commits(root, since):
        entries = commit_files(root, commit, prefix, excludes)
        wanted = {}
        for blob, relative in entries:
            if blob not in counted:
                wanted.setdefault(blob, relative)
        counted.update(read_blobs(root, wanted, commit))
        groups, _ = group_counts(root, test_patterns, [(r, counted[b]) for b, r in entries])
        days.append({"date": date, "commit": commit, "groups": groups})
    return days


def compare(current: dict, previous: dict) -> dict:
    for key in ("root", "options"):
        if current[key] != previous.get(key):
            raise ValueError(f"snapshot {key} differs; compare the same path and options")
    delta = {}
    for group in current["groups"]:
        delta[group] = {}
        for metric in METRICS:
            old = previous["groups"][group][metric]
            if type(old) is not int or old < 0:
                raise ValueError("snapshot metrics must be nonnegative integers")
            delta[group][metric] = current["groups"][group][metric] - old
    return delta


def print_table(rows: list[tuple[str, dict]], signed: bool = False) -> None:
    label_width = max(8, *(len(label) for label, _ in rows))
    print(f"{'Group':<{label_width}}" + "".join(f"{key.title():>11}" for key in METRICS))
    for label, counts in rows:
        values = (format(counts[key], "+,d" if signed else ",d") for key in METRICS)
        print(f"{label:<{label_width}}" + "".join(f"{value:>11}" for value in values))


def print_daily(days: list[dict]) -> None:
    header = "".join(f"{key.title():>11}" for key in METRICS)
    print(f"{'Date':<12}{'Commit':<12}{header}{'Code +/-':>11}")
    previous = None
    for day in days:
        counts = day["groups"]["total"]
        change = "" if previous is None else format(counts["code"] - previous, "+,d")
        values = "".join(f"{format(counts[key], ',d'):>11}" for key in METRICS)
        print(f"{day['date']:<12}{day['commit'][:10]:<12}{values}{change:>11}")
        previous = counts["code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path(__file__).resolve().parent.parent,
        help="directory to scan (default: Firm repo)",
    )
    parser.add_argument("--by-file", action="store_true", help="also show per-file counts")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude a path glob relative to the scan directory; repeatable",
    )
    parser.add_argument(
        "--test-pattern",
        action="append",
        default=[],
        metavar="GLOB",
        help="also classify matching paths as tests; repeatable",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="also count the last commit of each day in Git history",
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help="limit --daily to commits after a Git date, such as '90 days ago'",
    )
    parser.add_argument("--save", type=Path, metavar="FILE", help="write a JSON snapshot")
    parser.add_argument(
        "--compare", type=Path, metavar="FILE", help="show changes since a snapshot"
    )
    args = parser.parse_args(argv)
    root = args.path.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    if args.since and not args.daily:
        parser.error("--since requires --daily")
    try:
        current = report(root, args.exclude, args.test_pattern)
        if args.daily:
            current["daily"] = history(root, args.exclude, args.test_pattern, args.since)
        if args.compare:
            previous = json.loads(args.compare.read_text(encoding="utf-8"))
            current["delta"] = compare(current, previous)
        serialized = json.dumps(current, indent=2) + "\n"
        if args.save:
            args.save.write_text(serialized, encoding="utf-8")
    except (
        OSError,
        UnicodeError,
        SyntaxError,
        tokenize.TokenError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as error:
        parser.error(str(error))
    if args.json:
        print(serialized, end="")
    else:
        print(f"Python lines: {root}\n")
        print_table(list(current["groups"].items()))
        if args.by_file and current["files"]:
            print()
            print_table([(entry["path"], entry) for entry in current["files"]])
        if current.get("daily"):
            print("\nDaily totals at the last commit of each day:\n")
            print_daily(current["daily"])
        elif args.daily:
            print("\nNo commits found in Git history for this scan.")
        if "delta" in current:
            print(f"\nChange since {args.compare}:")
            print_table(list(current["delta"].items()), signed=True)
        print("\nComments include docstrings and inline comments; code/comment counts can overlap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
