"""The README and docs/ against the code they describe: the flags, the names and the links they give.

Each failure names the document and line that is wrong, as `file:line: what`.
"""

import argparse
import ast
import functools
import os
import pathlib
import re
import unittest
import urllib.parse
from unittest.mock import patch

import gate
import orchestrator

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = [README, *sorted((ROOT / "docs").glob("*.md"))]

FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")

# Commands and flags the docs give that the orchestrator does not have, by (document, command, flag); a flag of
# None stands for the command itself.
NOT_IN_THE_PARSER = {
    ("quality-gate.md", "gate", None): "main's gate before gate.py, which the Jenkinsfile's cutover fallback runs",
    ("roadmap.md", "resume", "--workflow"): "a planned feature, K",
    ("ui-options.md", "list", "--watch"): "a step of a staged plan, not built",
    ("ui-options.md", "web", None): "the assessment's name for the command, built as serve",
}

# Backticked ALL_CAPS names the docs give that are neither orchestrator.py's nor gate.py's, with where they come from.
OTHER_PROGRAMS_NAMES = {
    "PATH": "an environment variable",
    "PWD": "an environment variable",
    "OWD": "the AppImage runtime's environment variable",
    "PYTHONHOME": "Python's environment variable",
    "LD_LIBRARY_PATH": "the dynamic loader's environment variable",
    "PYTEST_ADDOPTS": "pytest's environment variable",
    "CARGO_HOME": "cargo's environment variable",
    "ORCHESTRATOR_PY": "the desktop app's environment variable, ui/src-tauri/src/lib.rs",
    "BUNDLED_NAME": "the desktop app's constant, ui/src-tauri/src/lib.rs",
    "PR_STATUS_CREATOR": "the Jenkinsfile's",
    "CHANGE_TARGET": "an environment variable Jenkins sets",
    "OPTIONS": "an HTTP method",
    "NAME": "the README's placeholder for a prompt's name",
}

# Backticked names the docs give that read as Class.member but are file names, with whose files they are.
FILE_NAMES = {
    "Cargo.toml": "cargo's manifest",
}


def built_parser(parse_args, argv) -> argparse.ArgumentParser:
    """The parser parse_args builds to parse argv, caught before it parses."""
    class Built(Exception):
        pass

    def stop(parser, *args, **kwargs):
        raise Built(parser)

    with patch.object(argparse.ArgumentParser, "parse_args", autospec=True, side_effect=stop):
        try:
            parse_args(argv)
        except Built as e:
            return e.args[0]
    raise AssertionError(f"{parse_args.__module__}.parse_args parsed without its parser's parse_args")


def long_flags(parser: argparse.ArgumentParser) -> set[str]:
    """The long flags parser takes; hidden ones left out."""
    return {o for a in parser._actions if a.help != argparse.SUPPRESS for o in a.option_strings if o.startswith("--")}


@functools.cache
def cli_commands() -> dict[str, set[str]]:
    """Each command of the parser main builds, by name, with the long flags it takes, and gate.py's, a program of
    its own, as the command "gate.py"."""
    parser = built_parser(orchestrator.parse_args, ["list"])
    (commands,) = [a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return {**{name: long_flags(sub) for name, sub in commands.items()},
            "gate.py": long_flags(built_parser(gate.parse_args, ["--base", "main"]))}


def prose_lines(path: pathlib.Path):
    """(line number, line, whether it is inside a fenced code block) for each line of a markdown file."""
    fenced = False
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        yield n, line, fenced


def invocations(path: pathlib.Path, commands):
    """(line number, command, the text of its arguments) for each command of the orchestrator's that path shows
    being run: in a code span or a fenced line, after `orchestrator.py`, or opening a code span, as in `run --spec`;
    and gate.py, as the command "gate.py", wherever it is followed by a flag. Shell comments, and what follows a
    pipe, `&&` or `;`, are left out."""
    for n, line, fenced in prose_lines(path):
        if fenced:
            texts = [] if line.lstrip().startswith("#") else [line]
        else:
            texts = re.findall(r"`([^`]+)`", line)
        for text in (re.split(r"\||&&|;|\s#", t)[0] for t in texts):
            for m in re.finditer(r"orchestrator\.py\s+([a-z][a-z-]*)(.*)", text):
                yield n, m.group(1), m.group(2)
            for m in re.finditer(r"\bgate\.py(\s+--.*)", text):
                yield n, "gate.py", m.group(1)
            words = text.split()
            # gate.py is not a command after orchestrator.py; the search above has yielded it.
            if not fenced and len(words) > 1 and words[0] in commands.keys() - {"gate.py"} \
                    and words[1].startswith("--"):
                yield n, words[0], text[len(words[0]):]


def flag_table(path: pathlib.Path):
    """(line number, flags, description) for each row of the table headed `| Flag | Description |`."""
    rows, inside = [], False
    for n, line, _ in prose_lines(path):
        if line.startswith("| Flag | Description |"):
            inside = True
        elif inside and line.startswith("|"):
            cells = line.split("|")
            if not set(cells[1].strip()) <= set("-"):
                rows.append((n, FLAG.findall(cells[1]), "|".join(cells[2:-1]).strip()))
        elif inside:
            break
    return rows


def source_names() -> tuple[set[str], set[str], set[str]]:
    """The names orchestrator.py and gate.py define: at module level; as Class.member, an attribute self is given
    included; and every string constant, which is how they name an environment variable or a Jenkins parameter."""
    tree = ast.Module(body=[node for source in ("orchestrator.py", "gate.py")
                            for node in ast.parse((ROOT / source).read_text()).body], type_ignores=[])

    def defined(body):
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                yield node.name
            elif isinstance(node, ast.Assign):
                yield from (t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                yield node.target.id

    members = set()
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        members.update(f"{cls.name}.{name}" for name in defined(cls.body))
        members.update(f"{cls.name}.{node.attr}" for node in ast.walk(cls)
                       if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                       and isinstance(node.value, ast.Name) and node.value.id == "self")
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    return set(defined(tree.body)), members, strings


@functools.cache
def github_anchors(path: pathlib.Path) -> set[str]:
    """The #anchors GitHub gives the headings of a markdown file: lowercased, punctuation dropped, spaces as
    hyphens, and -1, -2, ... after a repeated one."""
    anchors, seen = set(), {}
    for _, line, fenced in prose_lines(path):
        m = None if fenced else re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            slug = re.sub(r"[^\w\- ]", "", m.group(1).lower()).replace(" ", "-")
            anchors.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
            seen[slug] = seen.get(slug, 0) + 1
    return anchors


class TestDocs(unittest.TestCase):
    def assertNoErrors(self, errors):
        if errors:
            self.fail("\n" + "\n".join(errors))

    def test_each_command_the_docs_run_takes_the_flags_they_give_it(self):
        commands = cli_commands()
        errors = []
        for doc in DOCS:
            for n, command, rest in invocations(doc, commands):
                where = f"{doc.relative_to(ROOT)}:{n}"
                if command not in commands:
                    if (doc.name, command, None) not in NOT_IN_THE_PARSER:
                        errors.append(f"{where}: orchestrator.py has no command {command}")
                    continue
                errors.extend(f"{where}: {command} takes no {flag}" for flag in FLAG.findall(rest)
                              if flag not in commands[command] and (doc.name, command, flag) not in NOT_IN_THE_PARSER)
        self.assertNoErrors(errors)

    def test_the_readme_flag_table_gives_each_flag_the_commands_that_take_it(self):
        commands = cli_commands()
        rows = flag_table(README)
        self.assertGreater(len(rows), 10, "README.md's | Flag | Description | table")
        errors = []
        for n, flags, description in rows:
            # A description that opens with commands, as in "`run` and `drain`:" or "`run` only:", names the
            # commands that take the flag; "only" says no other one does.
            m = re.match(r"((?:`[a-z-]+`(?:,? and |,? or |, |/)?)+)( only)?:", description)
            scope = set(re.findall(r"`([a-z-]+)`", m.group(1))) if m else set()
            opening = re.match(r"`([a-z-]+)`", description)
            if not m and opening and opening.group(1) in commands:
                errors.append(f"README.md:{n}: the row opens with the command {opening.group(1)}, but not as a "
                              f"list of commands and a colon, which this test reads")
            for flag in flags:
                taking = {name for name, defined in commands.items() if flag in defined}
                if not taking:
                    errors.append(f"README.md:{n}: no command takes {flag}")
                elif not scope <= taking:
                    errors.append(f"README.md:{n}: {flag} is said to be for {', '.join(sorted(scope))}, but the "
                                  f"parser gives it to {', '.join(sorted(taking))}")
                elif m and m.group(2) and taking != scope:
                    errors.append(f"README.md:{n}: {flag} is said to be for {', '.join(sorted(scope))} only, but "
                                  f"the parser gives it to {', '.join(sorted(taking))}")
        self.assertNoErrors(errors)

    def test_each_flag_is_documented(self):
        # In the README or, for a command the README's table leaves out, such as gate.py, in docs/. A flag counts as
        # named wherever its name appears, said of any command or program.
        text = "\n".join(doc.read_text() for doc in DOCS)
        documented = set(FLAG.findall(text))
        missing = {f"{name} {flag}" for name, defined in cli_commands().items() for flag in defined - {"--help"}
                   if flag not in documented}
        self.assertEqual(missing, set(), "flags that neither README.md nor docs/*.md name")

    def test_the_names_the_docs_give_exist_in_the_orchestrator(self):
        names, members, strings = source_names()
        known = names | strings
        classes = {member.partition(".")[0] for member in members}
        methods = {member.partition(".")[2] for member in members}
        errors = []
        for doc in DOCS:
            for n, line, _ in prose_lines(doc):
                where = f"{doc.relative_to(ROOT)}:{n}"
                for code in re.findall(r"`([^`]+)`", line):
                    # A linter's rule code, such as C901 or BLE001, is not a name.
                    if re.fullmatch(r"[A-Z][A-Z0-9_]+", code) and not re.fullmatch(r"[A-Z]+[0-9]+", code):
                        if code not in known and code not in OTHER_PROGRAMS_NAMES:
                            errors.append(f"{where}: neither orchestrator.py nor gate.py has {code}")
                    elif m := re.fullmatch(r"(\w+)\(\)", code):
                        if m.group(1) not in names | methods:
                            errors.append(f"{where}: neither orchestrator.py nor gate.py has a function or method "
                                          f"{code}")
                    # Class.member: a capitalized name with a lowercase letter, or an all-caps one that is a class
                    # of orchestrator.py, such as CI; not CONSTANT.md, a prompt's file, nor one of FILE_NAMES.
                    elif code not in FILE_NAMES and (m := re.fullmatch(r"([A-Z]\w*)\.([a-z_]\w*)(\(\))?", code)) \
                            and (m.group(1) in classes or re.search("[a-z]", m.group(1))) \
                            and f"{m.group(1)}.{m.group(2)}" not in members:
                        errors.append(f"{where}: neither orchestrator.py nor gate.py has {m.group(1)}.{m.group(2)}")
        self.assertNoErrors(errors)

    def test_each_relative_link_resolves_to_a_file_and_its_anchor_to_a_heading(self):
        errors = []
        for doc in DOCS:
            for n, line, fenced in prose_lines(doc):
                if fenced:
                    continue
                prose = re.sub(r"`[^`]*`", "", line)
                for target in re.findall(r"\]\(([^)\s]+)\)", prose) + re.findall(r'(?:href|src)="([^"]+)"', prose):
                    if re.match(r"[a-z][a-z0-9+.-]*:", target):  # a URL, such as https: or mailto:
                        continue
                    path, _, anchor = target.partition("#")
                    # GitHub takes a path that starts with / from the repository's root.
                    base = ROOT if path.startswith("/") else doc.parent
                    dest = (base / urllib.parse.unquote(path.lstrip("/"))).resolve() if path else doc
                    where = f"{doc.relative_to(ROOT)}:{n}"
                    if not dest.exists():
                        errors.append(f"{where}: {target} names no file")
                    # The anchor of a file other than markdown, such as #L10 of a source file, names a line.
                    elif anchor and dest.suffix == ".md" and anchor not in github_anchors(dest):
                        errors.append(f"{where}: {target} names no heading of {os.path.relpath(dest, ROOT)}")
        self.assertNoErrors(errors)
