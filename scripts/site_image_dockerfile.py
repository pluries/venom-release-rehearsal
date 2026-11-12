#!/usr/bin/env python3
"""Checks of the site image Dockerfile (story #3622, task #3661). Standard library only."""
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_checks import PRIVATE_HOSTS  # noqa: E402

PINNED = re.compile(r"[\w./-]+@sha256:[0-9a-f]{64}")
PACKAGE_TOOLS = re.compile(r"\b(apk|apt|apt-get|curl|wget)\b")
PRIVATE_PATH = re.compile(r"/(?:home|Users|etc|root|mnt|opt|var)(?:/|$|\s)|\b[A-Za-z]:\\")
NUMERIC_USER = re.compile(r"(\d+)(?::(\d+))?")
HEREDOC = re.compile(r"<<-?\s*[\"']?\w")
DIRECTIVE = re.compile(r"#\s*(escape|syntax|check)\s*=", re.I)
ENV_KEYWORDS = ("ENV", "ARG", "LABEL")
WHOLE_CONTEXT = {".", "./", "/", "*", "./*", ".*"}


@dataclass
class Stage:
    from_line: str
    name: str | None
    instructions: list[tuple[str, str]] = field(default_factory=list)


def _logical_lines(text: str) -> list[str]:
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        if current.strip():
            lines.append(current.strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def _stage_name(args: str) -> str | None:
    match = re.search(r"\s+as\s+(\S+)\s*$", args, re.I)
    return match.group(1) if match else None


def _instructions(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in _logical_lines(text):
        keyword, _, args = line.partition(" ")
        pairs.append((keyword.upper(), args.strip()))
    return pairs


def _before_first_from(text: str) -> list[tuple[str, str]]:
    before = []
    for keyword, args in _instructions(text):
        if keyword == "FROM":
            break
        before.append((keyword, args))
    return before


def parse_stages(text: str) -> list[Stage]:
    stages: list[Stage] = []
    for line in _logical_lines(text):
        keyword, _, args = line.partition(" ")
        keyword, args = keyword.upper(), args.strip()
        if keyword == "FROM":
            stages.append(Stage(args, _stage_name(args)))
        elif stages:
            stages[-1].instructions.append((keyword, args))
    return stages


def _image_ref(from_args: str) -> str:
    tokens = [t for t in from_args.split() if not t.startswith("--")]
    return tokens[0] if tokens else ""


def _check_from(stages: list[Stage]) -> list[str]:
    return [f"from: {s.from_line!r} is not pinned as image@sha256:<64 hex> alone"
            for s in stages if not PINNED.fullmatch(_image_ref(s.from_line))]


def _copy_from(args: str) -> str | None:
    match = re.search(r"--from=(\S+)", args)
    return match.group(1) if match else None


def _check_final_copies(stages: list[Stage]) -> list[str]:
    earlier = {s.name for s in stages[:-1] if s.name}
    findings = []
    for keyword, args in stages[-1].instructions:
        if keyword == "ADD":
            findings.append(f"add: ADD in the final stage: {args}")
        if keyword == "COPY" and _copy_from(args) not in earlier:
            findings.append(f"copy: final-stage COPY without --from=<build stage>: {args}")
    return findings


def _copy_sources(args: str) -> list[str]:
    tokens = [t for t in args.split() if not t.startswith("--")]
    return tokens[:-1]


def _check_whole_context(stages: list[Stage]) -> list[str]:
    return [f"copy: {keyword} of the whole build context: {args}"
            for s in stages[:-1] for keyword, args in s.instructions
            if keyword in ("COPY", "ADD") and not _copy_from(args) and WHOLE_CONTEXT.intersection(_copy_sources(args))]


def _check_stages(stages: list[Stage]) -> list[str]:
    if len(stages) != 2:
        return [f"stages: expected exactly 2 stages, found {len(stages)}"]
    return _check_final_copies(stages) + _check_whole_context(stages)


def _user_ok(value: str) -> bool:
    match = NUMERIC_USER.fullmatch(value)
    return bool(match) and all(int(part) != 0 for part in match.groups() if part is not None)


def _check_user(final: Stage) -> list[str]:
    users = [args for keyword, args in final.instructions if keyword == "USER"]
    if not users:
        return ["user: the final stage sets no USER"]
    if not _user_ok(users[-1]):
        return [f"user: USER {users[-1]} is not a numeric non-zero uid[:gid]"]
    return []


def _check_install(final: Stage) -> list[str]:
    return [f"install: package or download tool in the final stage: {args}"
            for keyword, args in final.instructions if keyword == "RUN" and PACKAGE_TOOLS.search(args)]


def _check_env(text: str, stages: list[Stage]) -> list[str]:
    pairs = _before_first_from(text) + [pair for s in stages for pair in s.instructions]
    return [f"env: {keyword} carries a private host, address or path: {args}"
            for keyword, args in pairs
            if keyword in ENV_KEYWORDS and (PRIVATE_HOSTS.search(args) or PRIVATE_PATH.search(args))]


def _leading_directives(text: str) -> list[str]:
    found = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            break
        if DIRECTIVE.match(stripped):
            found.append(f"unsupported: parser directive {stripped!r}")
    return found


def _check_unsupported(text: str, stages: list[Stage]) -> list[str]:
    findings = _leading_directives(text)
    findings += [f"unsupported: {keyword} before the first FROM: {args}"
                 for keyword, args in _before_first_from(text) if keyword != "ARG"]
    findings += [f"unsupported: heredoc: {line}" for line in _logical_lines(text) if HEREDOC.search(line)]
    findings += [f"unsupported: ONBUILD {args}"
                 for s in stages for keyword, args in s.instructions if keyword == "ONBUILD"]
    return findings


def _check_license(final: Stage) -> list[str]:
    for keyword, args in final.instructions:
        if keyword == "COPY" and _copy_from(args) and "LICENSE" in args:
            return []
    return ["license: the final stage does not copy LICENSE from the build stage"]


def check_dockerfile(text: str) -> list[str]:
    stages = parse_stages(text)
    if not stages:
        return ["stages: no FROM found"]
    final = stages[-1]
    return (_check_from(stages) + _check_stages(stages) + _check_user(final) + _check_install(final)
            + _check_env(text, stages) + _check_license(final) + _check_unsupported(text, stages))
