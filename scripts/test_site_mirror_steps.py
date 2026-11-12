#!/usr/bin/env python3
"""Tests of the shell steps of the mirror publish job (story #3680, task #3692).
MirrorSteps covers AC-2, FR-18, NFR-01, NFR-02, NFR-03: jenkins/publish-steps.sh follows the shared library's command
contract (setup, dry-run, publish). git, git-filter-repo and node are stubs first on PATH, the publish script is a stub
given through PUBLISH_GITHUB_SCRIPT, and the script runs from a temporary copy of the jenkins folder.
Offline, standard library only. Run: python scripts/test_site_mirror_steps.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

BASH = shutil.which("bash")
JENKINS = ROOT / "jenkins"
SCRIPT = JENKINS / "publish-steps.sh"
USER = "mirror-user-example"
APPROVER = "approver-example"
FAKE_KEY = "-".join(("fake", "key", "body", "not", "real", "789"))
FULL_SHA = "abc1234" + "d" * 33
BRANCH = "publish-abc1234"
DRY_LINE = "DRY RUN: would push abc12345 to github:master"
FAILED_LINE = "ERROR: the dry run failed; nothing can be published"
NO_SUMMARY_LINE = "ERROR: the dry run printed no summary; nothing can be published"
COMPLETION = r"^stage github-publish completed: {sha} approved by {approver} at \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
GITOPS_NAMES = ("GITOPS_URL", "GITOPS_OVERLAY", "GITOPS_KNOWN_HOSTS")
GITOPS_KNOWN_HOSTS_TEXT = "git.example.test ssh-ed25519 AAAAfakehostkeybodynotreal123"
GITOPS_DEFAULTS = {"GITOPS_URL": "git@git.example.test:example/gitops.git",
                   "GITOPS_OVERLAY": "apps/venom/overlay.yaml",
                   "GITOPS_KNOWN_HOSTS": GITOPS_KNOWN_HOSTS_TEXT}
MANAGED_ENV = ("PUBLISH_SSH_KEY", "IM_MIRROR_USER", "IM_SHA_FULL", "PUBLISH_APPROVED_BY", "PUBLISH_DRY_RUN",
               "PUBLISH_GITHUB_SCRIPT", "GIT_SSH_COMMAND", "GITOPS_BRANCH", "PUBLISH_DRY_RUN_TIMEOUT") + GITOPS_NAMES

GIT_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/git.log"
case "$*" in
  "rev-parse HEAD"|"rev-parse --verify HEAD"|"rev-parse HEAD^{commit}") echo "$STUB_HEAD" ;;
  "rev-parse --short"*) echo "${STUB_HEAD:0:7}" ;;
  "remote get-url github")
    if [ -n "${STUB_REMOTE:-}" ]; then echo "$STUB_REMOTE"; else echo "error: No such remote 'github'" >&2; exit 2; fi ;;
  "remote") [ -n "${STUB_REMOTE:-}" ] && echo github ;;
esac
exit 0
"""

TOOL_STUB = """\
#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG_DIR/tools.log"
exit 0
"""

PUBLISH_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/publish.log"
printf 'DRY=%s\\nSSH=%s\\n' "${PUBLISH_DRY_RUN-unset}" "${GIT_SSH_COMMAND-unset}" >> "$STUB_LOG_DIR/publish.env"
echo "secret scan: clean"
if [ -n "${STUB_PUBLISH_FAIL:-}" ]; then
  echo "scan refused the content" >&2
  exit 1
fi
case "${STUB_PUBLISH_MODE:-}" in
  empty) exit 0 ;;
  kill) exit 137 ;;
  child) sleep 30 & echo $! > "$STUB_LOG_DIR/child.pid"; wait ;;
esac
if [ "${PUBLISH_DRY_RUN:-0}" = 1 ]; then echo "DRY RUN: would push abc12345 to github:master"; else echo "published snapshot"; fi
"""


def write_text(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def process_is_running(pid: str) -> bool:
    """A zombie is dead: a PID 1 that never reaps leaves the killed child listed, and kill -0 succeeds on it."""
    stat = Path("/proc") / pid / "stat"
    if not Path("/proc/self/stat").is_file():
        return subprocess.run([BASH, "-c", 'kill -0 "$1"', "_", pid]).returncode == 0
    try:
        state = stat.read_text(encoding="utf-8", errors="replace").rpartition(")")[2].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def child_is_alive(pid: str, wait_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + wait_seconds
    while process_is_running(pid):
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.1)
    return False


def path_without(tool: str) -> str:
    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        base = Path(entry)
        if any((base / (tool + ext)).exists() for ext in ("", ".exe", ".cmd", ".bat")):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


class Run:
    def __init__(self, completed: subprocess.CompletedProcess, root: Path, log_dir: Path):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.root = root
        self.git_calls = read_log(log_dir / "git.log").splitlines()
        self.tool_calls = read_log(log_dir / "tools.log").splitlines()
        self.publish_calls = read_log(log_dir / "publish.log").splitlines()
        self.publish_env = dict(line.split("=", 1) for line in read_log(log_dir / "publish.env").splitlines())
        self.summary = self.read_root("publish-summary.txt")
        self.report = self.read_root("publish-dry-run.txt")

    def read_root(self, name: str) -> str | None:
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def everything(self) -> str:
        return "\n".join([self.stdout, self.stderr, self.summary or "", self.report or ""])


def install_steps(root: Path, known_hosts: str | None, remote: str | None) -> Path:
    steps = root / "jenkins"
    steps.mkdir()
    shutil.copy(SCRIPT, steps / "publish-steps.sh")
    for name in ("github-known-hosts", "github-remote"):
        shutil.copy(JENKINS / name, steps / name)
    if known_hosts is not None:
        write_text(steps / "github-known-hosts", known_hosts)
    if remote is not None:
        write_text(steps / "github-remote", remote)
    return steps


def add_shims(stubs: Path, names: tuple[str, ...]) -> None:
    for name in names:
        real = Path(shutil.which(name)).as_posix()
        write_text(stubs / name, f'#!/bin/bash\nexec "{real}" "$@"\n', True)


def install_stubs(root: Path, missing: str, tools_only: bool) -> Path:
    stubs = root / "stubs"
    write_text(stubs / "git", GIT_STUB, True)
    for tool in ("git-filter-repo", "node"):
        if tool != missing:
            write_text(stubs / tool, TOOL_STUB, True)
    if missing == "git":
        (stubs / "git").unlink()
    if tools_only:
        add_shims(stubs, ("dirname", "grep"))
    return stubs


def steps_path(stubs: Path, missing: str, tools_only: bool) -> str:
    if tools_only:
        return str(stubs)
    return str(stubs) + os.pathsep + (path_without(missing) if missing else os.environ.get("PATH", ""))


def run_steps(root: Path, command: str | None, known_hosts: str | None = None, remote: str | None = None,
              missing: str = "", remote_present: bool = False, publish_fail: bool = False,
              drop: tuple[str, ...] = (), extra: dict[str, str] | None = None,
              seed_files: dict[str, str] | None = None, tools_only: bool = False) -> Run:
    """seed_files: files written into the tree before the step (the unstash is not simulated otherwise).
    tools_only: PATH holds only the stub directory (stubs plus shims for dirname and grep), so timeout is absent
    without dropping a directory that also holds the tools the script needs."""
    steps = install_steps(root, known_hosts, remote)
    stubs = install_stubs(root, missing, tools_only)
    log_dir = root / "logs"
    log_dir.mkdir()
    publish = root / "stub-publish.sh"
    write_text(publish, PUBLISH_STUB)
    key = root / "deploy-key"
    write_text(key, FAKE_KEY + "\n")
    for name, text in (seed_files or {}).items():
        write_text(root / name, text)
    env = {k: v for k, v in os.environ.items() if k not in MANAGED_ENV}
    env.update({"PUBLISH_SSH_KEY": key.as_posix(), "IM_MIRROR_USER": USER,
                "PUBLISH_GITHUB_SCRIPT": publish.as_posix()})
    env.update(GITOPS_DEFAULTS)
    for name in drop:
        env.pop(name, None)
    env.update(extra or {})
    env["PATH"] = steps_path(stubs, missing, tools_only)
    env["STUB_LOG_DIR"] = log_dir.as_posix()
    env["STUB_HEAD"] = FULL_SHA
    env["STUB_REMOTE"] = (JENKINS / "github-remote").read_text(encoding="utf-8").strip() if remote_present else ""
    env["STUB_PUBLISH_FAIL"] = "1" if publish_fail else ""
    argv = [BASH, (steps / "publish-steps.sh").as_posix()] + ([] if command is None else [command])
    completed = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
    return Run(completed, root, log_dir)


@unittest.skipUnless(BASH, "bash is not available")
class MirrorSteps(unittest.TestCase):
    """T1 to T5 of task #3692. Covers AC-2, FR-18, NFR-01, NFR-02, NFR-03."""

    def steps(self, command: str | None, **kwargs) -> Run:
        with tempfile.TemporaryDirectory() as tmp:
            return run_steps(Path(tmp), command, **kwargs)

    def remote_url(self) -> str:
        return (JENKINS / "github-remote").read_text(encoding="utf-8").strip()

    def test_T1_dry_run_writes_the_summary_and_the_report_and_exits_zero(self):
        result = self.steps("dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(DRY_LINE, result.summary or "")
        self.assertIn(DRY_LINE, result.report or "")
        self.assertIn("secret scan: clean", result.report or "")
        self.assertNotIn("ERROR", result.summary or "")

    def test_T1_dry_run_pushes_nothing_itself(self):
        result = self.steps("dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in result.git_calls if c.startswith("push")], [])
        self.assertEqual(result.publish_env.get("DRY"), "1")

    def test_T1_runs_the_publish_script_once_with_the_branch_and_the_snapshot_mode(self):
        result = self.steps("dry-run")
        self.assertEqual(result.publish_calls, [f"{BRANCH} master snapshot"])

    def test_T1_prepares_a_temporary_branch_at_head(self):
        result = self.steps("dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        branch_calls = [c for c in result.git_calls if c.startswith("branch")]
        self.assertEqual(len(branch_calls), 1, result.git_calls)
        self.assertRegex(branch_calls[0], rf"^branch -f {BRANCH}( HEAD| {FULL_SHA})?$")

    def test_T1_adds_the_github_remote_when_it_is_absent(self):
        result = self.steps("dry-run", remote_present=False)
        self.assertIn(f"remote add github {self.remote_url()}", result.git_calls)
        self.assertEqual([c for c in result.git_calls if c.startswith("remote set-url")], [])

    def test_T1_moves_the_github_remote_when_it_exists(self):
        result = self.steps("dry-run", remote_present=True)
        self.assertIn(f"remote set-url github {self.remote_url()}", result.git_calls)
        self.assertEqual([c for c in result.git_calls if c.startswith("remote add")], [])

    def test_T1_ssh_command_pins_the_key_and_the_known_hosts_file_and_checks_strictly(self):
        result = self.steps("dry-run")
        ssh = result.publish_env.get("SSH", "")
        match = re.fullmatch(
            r"ssh -i (\S+) -o IdentitiesOnly=yes -o UserKnownHostsFile=(\S+) -o StrictHostKeyChecking=yes", ssh)
        self.assertIsNotNone(match, ssh)
        self.assertTrue(match.group(1).endswith("/deploy-key"), ssh)
        self.assertTrue(match.group(2).endswith("/jenkins/github-known-hosts"), ssh)
        self.assertRegex(match.group(2), r"^(/|[A-Za-z]:)")
        self.assertNotIn("accept-new", ssh)

    def test_T1_a_matching_full_commit_id_is_accepted(self):
        result = self.steps("dry-run", extra={"IM_SHA_FULL": FULL_SHA})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_T1_a_commit_id_that_is_not_head_is_refused(self):
        result = self.steps("dry-run", extra={"IM_SHA_FULL": "f" * 40})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match HEAD", result.stderr)
        self.assertEqual(result.publish_calls, [])

    # Scenario US1-AS3: an approved dry run that failed publishes nothing (pinned existing test).
    def test_T2_dry_run_failure_is_non_zero_and_the_summary_says_nothing_can_be_published(self):
        result = self.steps("dry-run", publish_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.summary.splitlines()[0], FAILED_LINE)
        self.assertNotIn(DRY_LINE, result.summary)

    # Scenario US1-AS3: an approved dry run that failed publishes nothing (pinned existing test).
    def test_T2_dry_run_failure_keeps_the_full_log(self):
        result = self.steps("dry-run", publish_fail=True)
        self.assertIn("scan refused the content", result.report or "")

    def test_T3_publish_without_an_approver_is_refused_before_any_git_command(self):
        for extra in ({}, {"PUBLISH_APPROVED_BY": ""}):
            with self.subTest(extra=extra):
                result = self.steps("publish", extra=extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PUBLISH_APPROVED_BY", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(result.publish_calls, [])

    def test_T3_publish_with_an_approver_runs_the_script_once_for_real(self):
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.publish_calls, [f"{BRANCH} master snapshot"])
        self.assertEqual(result.publish_env.get("DRY"), "unset")
        self.assertIn(f"publish-steps: published by {APPROVER}", result.stdout)

    def test_T3_publish_prepares_the_branch_and_the_remote_like_the_dry_run(self):
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER})
        self.assertIn(f"remote add github {self.remote_url()}", result.git_calls)
        self.assertRegex(result.publish_env.get("SSH", ""), r"StrictHostKeyChecking=yes")
        branch_calls = [c for c in result.git_calls if c.startswith("branch")]
        self.assertEqual(len(branch_calls), 1, result.git_calls)
        self.assertRegex(branch_calls[0], rf"^branch -f {BRANCH}( HEAD| {FULL_SHA})?$")

    def test_T3_publish_exits_with_a_failing_script_status(self):
        result = self.steps("publish", publish_fail=True, extra={"PUBLISH_APPROVED_BY": APPROVER})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("published by", result.stdout)

    def test_T3_publish_refuses_a_commit_id_that_is_not_head(self):
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER, "IM_SHA_FULL": "f" * 40})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match HEAD", result.stderr)
        self.assertEqual(result.publish_calls, [])

    def test_T4_setup_succeeds_when_the_tools_and_the_data_files_are_present(self):
        result = self.steps("setup", drop=("PUBLISH_SSH_KEY", "IM_MIRROR_USER"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_T4_setup_reads_no_credential_and_makes_no_network_call(self):
        result = self.steps("setup", extra={"PUBLISH_SSH_KEY": "/nonexistent/key"})
        self.assertEqual(result.returncode, 0, result.stderr)
        network = ("fetch", "clone", "push", "ls-remote", "pull")
        self.assertEqual([c for c in result.git_calls if c.split(" ")[0] in network], [])
        self.assertEqual(result.publish_calls, [])

    def test_T4_setup_names_a_missing_tool(self):
        for tool in ("git", "git-filter-repo", "node"):
            with self.subTest(tool=tool):
                result = self.steps("setup", missing=tool)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(tool, result.stderr)

    def test_T4_setup_refuses_a_known_hosts_file_without_a_host_line(self):
        for text in ("", "# only a comment\n\n"):
            with self.subTest(text=text):
                result = self.steps("setup", known_hosts=text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("github-known-hosts", result.stderr)

    def test_T4_setup_refuses_a_remote_file_that_is_not_one_line(self):
        two = "git@github.example.test:example/one.git\ngit@github.example.test:example/two.git\n"
        for text in (two, "\n"):
            with self.subTest(text=text):
                result = self.steps("setup", remote=text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("github-remote", result.stderr)

    def test_review_setup_refuses_a_missing_empty_or_blank_gitops_parameter_naming_it(self):
        for name in GITOPS_NAMES:
            for mode, value in (("unset", None), ("empty", ""), ("spaces", "   "), ("tab", "\t ")):
                with self.subTest(name=name, mode=mode):
                    result = self.steps("setup", drop=(name,) if value is None else (),
                                        extra={} if value is None else {name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.git_calls, [])
                    self.assertEqual(result.publish_calls, [])

    def test_review_setup_refuses_a_gitops_parameter_starting_with_a_dash_naming_it_not_its_value(self):
        value = "-oProxyCommand=example-not-real"
        for name in GITOPS_NAMES:
            with self.subTest(name=name):
                result = self.steps("setup", extra={name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertNotIn(value, result.everything())

    def test_review_setup_refuses_a_dash_after_leading_whitespace(self):
        result = self.steps("setup", extra={"GITOPS_URL": "  -oProxyCommand=example-not-real"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GITOPS_URL", result.stderr)

    def test_review_setup_does_not_require_the_optional_gitops_branch(self):
        result = self.steps("setup", drop=("GITOPS_BRANCH",))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_setup_does_not_check_the_format_of_the_values(self):
        result = self.steps("setup", extra={"GITOPS_URL": "not a url", "GITOPS_OVERLAY": "../odd"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_dry_run_and_publish_do_not_require_the_gitops_parameters(self):
        for command in ("dry-run", "publish"):
            with self.subTest(command=command):
                result = self.steps(command, drop=GITOPS_NAMES, extra={"PUBLISH_APPROVED_BY": APPROVER})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_review_the_gitops_known_hosts_text_never_reaches_the_output(self):
        ok = self.steps("setup")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn(GITOPS_KNOWN_HOSTS_TEXT, ok.everything())
        refused = self.steps("setup", extra={"GITOPS_URL": ""})
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn(GITOPS_KNOWN_HOSTS_TEXT, refused.everything())
        refused = self.steps("setup", extra={"GITOPS_KNOWN_HOSTS": "-" + GITOPS_KNOWN_HOSTS_TEXT})
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn(GITOPS_KNOWN_HOSTS_TEXT, refused.everything())

    def test_T5_the_key_content_never_reaches_the_output_or_the_log_files(self):
        dry = self.steps("dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertNotIn(FAKE_KEY, dry.everything())
        real = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER})
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertNotIn(FAKE_KEY, real.everything())

    def test_T5_an_unknown_command_or_none_prints_usage_and_runs_no_git(self):
        for command in ("frobnicate", None):
            with self.subTest(command=command):
                result = self.steps(command)
                self.assertEqual(result.returncode, 2)
                self.assertRegex(result.stderr.lower(), "usage")
                self.assertEqual(result.git_calls, [])

    def test_T5_a_missing_or_empty_input_is_rejected_before_any_git_command(self):
        for name in ("PUBLISH_SSH_KEY", "IM_MIRROR_USER"):
            for command in ("dry-run", "publish"):
                for mode in ("unset", "empty"):
                    with self.subTest(name=name, command=command, mode=mode):
                        extra = {"PUBLISH_APPROVED_BY": APPROVER}
                        if mode == "empty":
                            extra[name] = ""
                        result = self.steps(command, extra=extra, drop=(name,) if mode == "unset" else ())
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(name, result.stderr)
                        self.assertEqual(result.git_calls, [])
                        self.assertEqual(result.publish_calls, [])

    def completion_pattern(self) -> str:
        return COMPLETION.format(sha=FULL_SHA, approver=re.escape(APPROVER))

    def test_a_dry_run_that_exits_zero_without_the_dry_run_line_fails_and_says_so(self):
        """Task T005, US1-AS12, FR-001."""
        result = self.steps("dry-run", extra={"STUB_PUBLISH_MODE": "empty"})
        self.assertNotEqual(result.returncode, 0, "a dry run with no DRY RUN line was accepted")
        self.assertEqual((result.summary or "").splitlines()[:1], [NO_SUMMARY_LINE])

    @unittest.skipIf(sys.platform == "win32" or not shutil.which("timeout"),
                     "process-group kill is unreliable on MSYS, or timeout is not on PATH")
    def test_a_dry_run_over_the_time_limit_fails_and_the_children_are_killed(self):
        """Task T005, US1-AS12, FR-001: limit 1 second, the stub starts a child that sleeps 30 seconds."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_steps(Path(tmp), "dry-run",
                               extra={"STUB_PUBLISH_MODE": "child", "PUBLISH_DRY_RUN_TIMEOUT": "1"})
            pid = read_log(Path(tmp) / "logs" / "child.pid").strip()
            alive = bool(pid) and child_is_alive(pid)
            if alive:
                subprocess.run([BASH, "-c", 'kill "$1"', "_", pid])
        self.assertNotEqual(result.returncode, 0, "a dry run over the time limit was accepted")
        self.assertEqual((result.summary or "").splitlines()[:1], [FAILED_LINE])
        self.assertFalse(alive, "the sleeping child outlived the time limit")

    def test_a_non_numeric_or_zero_time_limit_is_refused_before_any_git_call(self):
        """Task T005, FR-001: the limit matches ^[1-9][0-9]*$ or nothing runs."""
        for value in ("abc", "0", "00", "-5", "1.5", "10s", " 5"):
            with self.subTest(limit=value):
                result = self.steps("dry-run", extra={"PUBLISH_DRY_RUN_TIMEOUT": value})
                self.assertNotEqual(result.returncode, 0, f"the limit {value!r} was accepted")
                self.assertIn("PUBLISH_DRY_RUN_TIMEOUT", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(result.publish_calls, [])

    def test_a_killed_dry_run_exits_non_zero_with_the_error_summary(self):
        """Task T005, US1-AS12 (pinned characterisation): a stub that exits 137 is a failed dry run."""
        result = self.steps("dry-run", extra={"STUB_PUBLISH_MODE": "kill"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((result.summary or "").splitlines()[:1], [FAILED_LINE])

    def test_setup_fails_naming_timeout_when_it_is_not_on_path(self):
        """Task T005, T011: the PATH holds only the stub directory (git, git-filter-repo, node, dirname, grep)."""
        result = self.steps("setup", tools_only=True)
        self.assertNotEqual(result.returncode, 0, "setup passed without timeout on PATH")
        self.assertIn("timeout", result.stderr)

    def test_publish_adds_the_completion_line_to_the_output_and_to_publish_summary_txt(self):
        """Task T007, US1-AS13, FR-008."""
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER},
                            seed_files={"publish-summary.txt": DRY_LINE + "\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        done = [i for i, line in enumerate(lines) if re.search(self.completion_pattern(), line)]
        self.assertEqual(len(done), 1, lines)
        self.assertGreater(done[0], lines.index(f"publish-steps: published by {APPROVER}"))
        self.assertRegex(result.summary or "", re.compile(self.completion_pattern(), re.M))

    def test_publish_keeps_its_existing_published_by_line_unchanged(self):
        """Task T007, FR-008 (pinned characterisation): the line is exact, alone on its line."""
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"publish-steps: published by {APPROVER}", result.stdout.splitlines())

    def test_a_failing_publish_writes_no_completion_line(self):
        """Task T007, US1-AS13 (green today, stays green): the seeded summary is left as it was."""
        seed = DRY_LINE + "\n"
        result = self.steps("publish", publish_fail=True, extra={"PUBLISH_APPROVED_BY": APPROVER},
                            seed_files={"publish-summary.txt": seed})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("github-publish completed", result.everything())
        self.assertEqual(result.summary, seed)

    def test_publish_refuses_an_approver_outside_the_allowed_characters_before_any_git_command(self):
        """Task T007, NFR-004: the approver matches ^[A-Za-z0-9._@-]{1,80}$."""
        for value in ("two words", "semi;colon", "$(id)", "back`tick`", "café", "a" * 81):
            with self.subTest(approver=value):
                result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": value})
                self.assertNotEqual(result.returncode, 0, f"the approver {value!r} was accepted")
                self.assertIn("PUBLISH_APPROVED_BY", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(result.publish_calls, [])

    def test_publish_appends_after_the_dry_run_lines_in_publish_summary_txt(self):
        """Task T007, FR-008: the unstashed dry-run lines stay, the completion line follows."""
        seed = "secret scan: clean\n" + DRY_LINE + "\n"
        result = self.steps("publish", extra={"PUBLISH_APPROVED_BY": APPROVER},
                            seed_files={"publish-summary.txt": seed})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((result.summary or "").startswith(seed), result.summary)
        self.assertRegex((result.summary or "").rstrip("\n").splitlines()[-1], self.completion_pattern())

    def test_T5_the_script_is_valid_bash(self):
        checked = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_T5_the_script_text_holds_no_host_literal_and_no_trace(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"github\.|git@|ssh-ed25519|ssh-rsa|ecdsa-sha2")
        self.assertNotRegex(text, r"set\s+-\w*x|set\s+-o\s+xtrace")
        self.assertNotIn("accept-new", text)


if __name__ == "__main__":
    unittest.main()
