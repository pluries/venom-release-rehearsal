#!/usr/bin/env python3
"""Tests of the shell steps of the site publish job (story #3623, reworked by story #3680).
ImageSteps (task #3690) covers AC-3, FR-10, NFR-01, NFR-04: jenkins/image-steps.sh follows the shared library's
command contract. `build` runs make site-image-check; `push` logs in, builds and pushes the site image, then writes
the pushed digest reference to image-digest.txt. docker, make and git are stubs first on PATH.
GitopsSteps (task #3691) covers AC-4, FR-10, NFR-01, NFR-07, NFR-12: `gitops-steps.sh update` follows the shared
library's command contract and changes only the digest line of the venom overlay in a local bare repository, through
real git behind a logging wrapper.
Offline, standard library only. Run: python scripts/test_site_publish_steps.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

BASH = shutil.which("bash")
IMAGE_STEPS = ROOT / "jenkins" / "image-steps.sh"
DIGEST_FILE = ROOT / "image-digest.txt"
REGISTRY = "registry.example.test"
REPOSITORY = "venom-site"
USER = "ci-user-example"
FAKE_PASSPHRASE = "-".join(("pw", "not", "a", "secret", "123"))
DIGEST = "c" * 64
SHORT_SHA = "abc1234"
APPROVER = "approver-example"
FULL_SHA = SHORT_SHA + "d" * 33
IMAGE = f"{REGISTRY}/{REPOSITORY}"
INPUTS = {
    "IM_REGISTRY_USER": USER,
    "IM_REGISTRY_PASS": FAKE_PASSPHRASE,
    "IM_IMAGE_REPOSITORY": IMAGE,
    "IM_SHA_FULL": FULL_SHA,
}

DOCKER_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/docker.log"
printf '%s|%s\\n' "${DOCKER_CONFIG-unset}" "$([ -d "${DOCKER_CONFIG:-/nonexistent}" ] && echo dir || echo nodir)" >> "$STUB_LOG_DIR/docker.config"
if [ "${STUB_FAIL:-}" = "$1" ]; then
  echo "docker $1 refused" >&2
  exit 1
fi
case "$1" in
  login) cat > "$STUB_LOG_DIR/login.stdin" ;;
  build) echo "build output" ;;
  push) printf '%s\\n' "${STUB_PUSH_OUTPUT:-}" ;;
esac
"""

MAKE_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/make.log"
if [ -n "${STUB_MAKE_FAIL:-}" ]; then
  echo "make refused" >&2
  exit 1
fi
"""

GIT_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/git.log"
echo "git stub: unexpected call: $*" >&2
exit 1
"""


def push_output(digest: str = DIGEST) -> str:
    return f"The push refers to repository [{IMAGE}]\nsha-{SHORT_SHA}: digest: sha256:{digest} size: 1234"


def write_stub(directory: Path, name: str, text: str) -> None:
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def make_stubs(root: Path) -> Path:
    stubs = root / "stubs"
    stubs.mkdir()
    write_stub(stubs, "docker", DOCKER_STUB)
    write_stub(stubs, "make", MAKE_STUB)
    write_stub(stubs, "git", GIT_STUB)
    return stubs


def read_log(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class Run:
    def __init__(self, completed: subprocess.CompletedProcess, log_dir: Path):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.docker_log = read_log(log_dir / "docker.log")
        self.docker_calls = self.docker_log.splitlines()
        self.make_calls = read_log(log_dir / "make.log").splitlines()
        self.git_calls = read_log(log_dir / "git.log").splitlines()
        self.login_stdin = read_log(log_dir / "login.stdin")
        self.docker_configs = [tuple(line.split("|")) for line in read_log(log_dir / "docker.config").splitlines()]
        self.scratch_entries: list[Path] = []
        self.digest_file: str | None = DIGEST_FILE.read_text(encoding="utf-8") if DIGEST_FILE.is_file() else None


def bash_dir_exists(path: str) -> bool:
    return subprocess.run([BASH, "-c", 'test -d "$1"', "_", path]).returncode == 0


def run_script(script: Path, workdir: Path, command: str | None = "push", push: str | None = None, fail: str = "",
               drop: tuple[str, ...] = (), extra: dict[str, str] | None = None, make_fail: bool = False) -> Run:
    stubs = make_stubs(workdir)
    log_dir = workdir / "logs"
    log_dir.mkdir()
    scratch = workdir / "scratch"
    scratch.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in INPUTS}
    env.update({k: v for k, v in INPUTS.items() if k not in drop})
    env.update(extra or {})
    env["TMPDIR"] = scratch.as_posix()
    env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
    env["STUB_LOG_DIR"] = log_dir.as_posix()
    env["STUB_PUSH_OUTPUT"] = push_output() if push is None else push
    env["STUB_FAIL"] = fail
    env["STUB_MAKE_FAIL"] = "1" if make_fail else ""
    argv = [BASH, script.as_posix()] + ([] if command is None else [command])
    completed = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True, encoding="utf-8",
                               timeout=60)
    result = Run(completed, log_dir)
    result.scratch_entries = list(scratch.iterdir())
    return result


@unittest.skipUnless(BASH, "bash is not available")
# Scenario US1-AS7: stage scripts, one commit per new digest, none for the same digest (pinned existing tests).
class ImageSteps(unittest.TestCase):
    """T1 to T5 of task #3690. Covers AC-3, FR-10, NFR-01, NFR-04."""

    def setUp(self):
        DIGEST_FILE.unlink(missing_ok=True)
        self.addCleanup(DIGEST_FILE.unlink, True)

    def run_steps(self, **kwargs) -> Run:
        with tempfile.TemporaryDirectory() as tmp:
            return run_script(IMAGE_STEPS, Path(tmp), **kwargs)

    def test_T1_writes_one_digest_reference_line_and_exits_zero(self):
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.digest_file, f"{IMAGE}@sha256:{DIGEST}\n")

    def test_T1_logs_in_builds_and_pushes_in_that_order(self):
        result = self.run_steps()
        self.assertEqual(result.docker_calls, [
            f"login -u {USER} --password-stdin {REGISTRY}",
            f"build -f deploy/site/Dockerfile -t {IMAGE}:sha-{SHORT_SHA} .",
            f"push {IMAGE}:sha-{SHORT_SHA}",
        ])

    def test_T1_a_seven_character_commit_id_gives_the_same_tag(self):
        result = self.run_steps(extra={"IM_SHA_FULL": SHORT_SHA})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.docker_calls[1], f"build -f deploy/site/Dockerfile -t {IMAGE}:sha-{SHORT_SHA} .")

    def test_T1_the_registry_host_is_the_part_before_the_first_slash(self):
        repository = f"{REGISTRY}:5000/team/venom-site"
        result = self.run_steps(extra={"IM_IMAGE_REPOSITORY": repository})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.docker_calls[0], f"login -u {USER} --password-stdin {REGISTRY}:5000")
        self.assertEqual(result.docker_calls[2], f"push {repository}:sha-{SHORT_SHA}")

    def test_T1_password_reaches_login_on_stdin(self):
        result = self.run_steps()
        self.assertEqual(result.login_stdin.strip(), FAKE_PASSPHRASE)

    def test_T1_other_push_output_lines_are_ignored(self):
        noisy = f"Pushed\nlayer: digest: sha256:{'d' * 64} size: 1\n" + push_output()
        result = self.run_steps(push=noisy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.digest_file, f"{IMAGE}@sha256:{DIGEST}\n")

    def test_T1_a_stale_digest_file_is_replaced(self):
        DIGEST_FILE.write_text(f"{IMAGE}@sha256:{'9' * 64}\n", encoding="utf-8", newline="\n")
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.digest_file, f"{IMAGE}@sha256:{DIGEST}\n")

    def test_T1_no_git_command_is_run(self):
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.git_calls, [])

    def test_T2_no_digest_fails_with_a_message_and_writes_no_file(self):
        result = self.run_steps(push="The push refers to repository\nlatest: pushed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(result.digest_file)
        self.assertIn("digest", result.stderr.lower())
        self.assertEqual(len(result.docker_calls), 3)

    def test_T2_malformed_digests_fail_with_a_message_and_write_no_file(self):
        bad = {
            "short": "c" * 63,
            "long": "c" * 65,
            "upper case": "C" * 64,
            "not hex": "g" * 64,
            "empty": "",
        }
        for name, digest in bad.items():
            with self.subTest(case=name):
                result = self.run_steps(push=push_output(digest))
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(result.digest_file)
                self.assertIn("digest", result.stderr.lower())
                self.assertEqual(len(result.docker_calls), 3)

    def test_T2_a_failing_docker_command_fails_and_writes_no_file(self):
        for step in ("login", "build", "push"):
            with self.subTest(step=step):
                result = self.run_steps(fail=step)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(result.digest_file)
                self.assertIn(step, result.stderr)

    def test_T2_a_stale_digest_file_does_not_survive_a_failure(self):
        for step in ("login", "build", "push"):
            with self.subTest(step=step):
                DIGEST_FILE.write_text(f"{IMAGE}@sha256:{'9' * 64}\n", encoding="utf-8", newline="\n")
                result = self.run_steps(fail=step)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(result.digest_file)
        DIGEST_FILE.write_text(f"{IMAGE}@sha256:{'9' * 64}\n", encoding="utf-8", newline="\n")
        result = self.run_steps(push="no digest here")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(result.digest_file)

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_password_is_only_on_the_stub_stdin_log(self):
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.login_stdin.strip(), FAKE_PASSPHRASE)
        self.assertNotIn(FAKE_PASSPHRASE, result.stdout)
        self.assertNotIn(FAKE_PASSPHRASE, result.stderr)
        self.assertNotIn(FAKE_PASSPHRASE, result.docker_log)
        self.assertNotIn(FAKE_PASSPHRASE, result.digest_file or "")

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_password_stays_out_of_the_output_when_a_step_fails(self):
        for step in ("login", "build", "push"):
            with self.subTest(step=step):
                result = self.run_steps(fail=step)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.docker_calls and result.docker_calls[-1].startswith(step + " "))
                self.assertNotIn(FAKE_PASSPHRASE, result.stdout)
                self.assertNotIn(FAKE_PASSPHRASE, result.stderr)
                self.assertNotIn(FAKE_PASSPHRASE, result.docker_log)

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_docker_config_is_a_fresh_directory_that_overrides_the_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = (Path(tmp) / "shared-docker-config").as_posix()
            result = run_script(IMAGE_STEPS, Path(tmp), extra={"DOCKER_CONFIG": shared})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.docker_configs), 3)
            values = {value for value, _ in result.docker_configs}
            self.assertEqual(len(values), 1)
            self.assertNotIn(shared, values)
            self.assertNotIn("unset", values)
            self.assertEqual({state for _, state in result.docker_configs}, {"dir"})

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_docker_config_is_a_fresh_directory_when_the_caller_sets_none(self):
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.docker_configs), 3)
        self.assertEqual({state for _, state in result.docker_configs}, {"dir"})

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_docker_config_directory_is_removed_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_script(IMAGE_STEPS, Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({state for _, state in result.docker_configs}, {"dir"})
            for value, _ in result.docker_configs:
                self.assertFalse(bash_dir_exists(value), value)
            self.assertEqual(result.scratch_entries, [])

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T3_docker_config_directory_is_removed_after_a_failing_step(self):
        for step in ("login", "build", "push"):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                result = run_script(IMAGE_STEPS, Path(tmp), fail=step)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.docker_configs)
                self.assertNotIn("unset", {value for value, _ in result.docker_configs})
                for value, _ in result.docker_configs:
                    self.assertFalse(bash_dir_exists(value), value)
                self.assertEqual(result.scratch_entries, [])

    def test_T4_a_missing_or_empty_input_stops_before_any_docker_call(self):
        for name in INPUTS:
            for how, kwargs in (("missing", {"drop": (name,)}), ("empty", {"extra": {name: ""}})):
                with self.subTest(variable=name, case=how):
                    result = self.run_steps(**kwargs)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.docker_calls, [])
                    self.assertIsNone(result.digest_file)
                    self.assertNotIn(FAKE_PASSPHRASE, result.stderr)

    def test_T4_an_unsafe_repository_is_rejected_before_any_docker_call(self):
        bad = ("-x", "--config=y", "a b", "a;b", "a$b", 'a"b', "a'b", "a`b`", "a|b")
        for value in bad:
            with self.subTest(value=value):
                result = self.run_steps(extra={"IM_IMAGE_REPOSITORY": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("IM_IMAGE_REPOSITORY", result.stderr)
                self.assertEqual(result.docker_calls, [])
                self.assertIsNone(result.digest_file)

    def test_T4_a_malformed_commit_id_is_rejected_before_any_docker_call(self):
        bad = {
            "short": "abc123",
            "long": "a" * 41,
            "upper case": "ABC1234" + "d" * 33,
            "not hex": "xyz1234",
            "dash": "-abc1234",
            "space": "abc1234 ",
        }
        for name, value in bad.items():
            with self.subTest(case=name):
                result = self.run_steps(extra={"IM_SHA_FULL": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("IM_SHA_FULL", result.stderr)
                self.assertEqual(result.docker_calls, [])
                self.assertIsNone(result.digest_file)

    def test_T4_an_unknown_or_missing_command_prints_usage_and_exits_two(self):
        for command in ("deploy", "", "Push", None):
            with self.subTest(command=command):
                result = self.run_steps(command=command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr.lower())
                self.assertEqual(result.docker_calls, [])
                self.assertEqual(result.make_calls, [])
                self.assertIsNone(result.digest_file)

    def test_T5_build_runs_make_site_image_check_and_nothing_else(self):
        result = self.run_steps(command="build", drop=tuple(INPUTS))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.make_calls, ["site-image-check"])
        self.assertEqual(result.docker_calls, [])
        self.assertEqual(result.git_calls, [])
        self.assertIsNone(result.digest_file)

    def test_T5_a_failing_make_fails_the_build_command(self):
        result = self.run_steps(command="build", drop=tuple(INPUTS), make_fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.make_calls, ["site-image-check"])
        self.assertEqual(result.docker_calls, [])

    def test_T5_push_does_not_run_make(self):
        result = self.run_steps()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.make_calls, [])

    def test_T5_script_is_valid_bash_and_strict(self):
        self.assertTrue(IMAGE_STEPS.is_file(), f"{IMAGE_STEPS.relative_to(ROOT)} does not exist")
        checked = subprocess.run([BASH, "-n", IMAGE_STEPS.as_posix()], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        text = IMAGE_STEPS.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("set -euo pipefail", text)
        self.assertIsNone(re.search(r"^\s*set\s+-\S*x", text, re.M))

    def test_T5_script_reads_its_inputs_from_the_environment(self):
        text = IMAGE_STEPS.read_text(encoding="utf-8")
        for name in INPUTS:
            with self.subTest(variable=name):
                self.assertIn(name, text)
        self.assertIn("--password-stdin", text)

    def test_T5_script_holds_no_host_namespace_or_credential_literal(self):
        text = IMAGE_STEPS.read_text(encoding="utf-8")
        host = re.search(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.(com|dev|io|net|org|test|local|cloud|fr|de)\b", text)
        self.assertIsNone(host, host and host.group(0))
        self.assertNotIn("pluries", text.lower())
        credential = re.search(r"(?i)(password|passwd|token|secret)\s*=\s*[\"']?[A-Za-z0-9]", text)
        self.assertIsNone(credential, credential and credential.group(0))
        self.assertIsNone(re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", text))


GITOPS_STEPS = ROOT / "jenkins" / "gitops-steps.sh"
REAL_GIT = shutil.which("git")
OVERLAY_VENOM = "apps/venom/overlay.yaml"
OVERLAY_OTHER = "apps/other/overlay.yaml"
OLD_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
NEW_DIGEST = "sha256:0123456789ab" + "e" * 52
IMAGE_REF = f"{IMAGE}@{NEW_DIGEST}"
KNOWN_HOSTS = "git.example.test ssh-ed25519 AAAAfakehostkeyexample"
KEY_CONTENT = "-".join(("fake", "key", "content", "not", "a", "secret"))
FIXTURE_GIT_CONFIG = "[user]\n\tname = fixture user\n\temail = fixture@example.invalid\n[init]\n\tdefaultBranch = main\n"

GIT_WRAPPER = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GIT_CALL_LOG"
if [ -n "${GIT_SSH_COMMAND:-}" ]; then
  kh=$(printf '%s' "$GIT_SSH_COMMAND" | sed -n 's/.*UserKnownHostsFile=\\([^ ]*\\).*/\\1/p')
  if [ -f "$kh" ]; then exists=1; content=$(tr '\\n' '|' < "$kh"); else exists=0; content=; fi
  printf '%s\\t%s\\t%s\\t%s\\n' "$GIT_SSH_COMMAND" "$kh" "$exists" "$content" >> "$GIT_SSH_LOG"
fi
for arg in "$@"; do
  if [ "$arg" = push ] && [ -n "${RIVAL_MODE:-}" ]; then
    n=$(cat "$RIVAL_STATE" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$RIVAL_STATE"
    if [ "$RIVAL_MODE" = every ] || [ "$n" = 1 ]; then
      "$REAL_GIT" -C "$RIVAL_DIR" pull -q --rebase origin main
      sed -i "s/digest: .*/digest: sha256:$(printf '%064d' "$n")/" "$RIVAL_DIR/apps/other/overlay.yaml"
      "$REAL_GIT" -C "$RIVAL_DIR" commit -q -am "rival $n"
      "$REAL_GIT" -C "$RIVAL_DIR" push -q origin HEAD:main
    fi
    break
  fi
done
exec "$REAL_GIT" "$@"
"""


def overlay_text(name: str, *digests: str) -> str:
    lines = [f"app: {name}", "image:", f"  repository: {REGISTRY}/{name}"]
    lines += [f"  digest: {d}" for d in digests]
    lines += ["  pullPolicy: IfNotPresent", ""]
    return "\n".join(lines)


class GitopsRun:
    def __init__(self, completed: subprocess.CompletedProcess, git_log: str, ssh_log: str = ""):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.git_log = git_log
        self.git_calls = git_log.splitlines()
        self.ssh_calls = [line.split("\t") for line in ssh_log.splitlines()]

    def calls(self, pattern: str) -> list[str]:
        return [c for c in self.git_calls if re.search(pattern, c)]


class GitopsFixture:
    def __init__(self, tmp: Path, venom: str | None = None):
        self.tmp = tmp
        self.bare = tmp / "remote.git"
        self.url = self.bare.as_uri()
        self.scratch = tmp / "scratch"
        self.scratch.mkdir()
        stubs = tmp / "stubs"
        stubs.mkdir()
        write_stub(stubs, "git", GIT_WRAPPER)
        self.stubs = stubs
        self.git_log = tmp / "git.log"
        self.ssh_log = tmp / "ssh.log"
        self.rival_state = tmp / "rival.state"
        self.rival = tmp / "rival"
        self.key = tmp / "deploy.key"
        self.key.write_text(KEY_CONTENT, encoding="utf-8", newline="\n")
        config = tmp / "gitconfig"
        config.write_text(FIXTURE_GIT_CONFIG, encoding="utf-8", newline="\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update({"GIT_CONFIG_GLOBAL": config.as_posix(), "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_TERMINAL_PROMPT": "0"})
        self.git("init", "--bare", "-b", "main", str(self.bare))
        seed = tmp / "seed"
        self.git("init", "-b", "main", str(seed))
        files = {OVERLAY_OTHER: overlay_text("other", OTHER_DIGEST),
                 OVERLAY_VENOM: venom if venom is not None else overlay_text("venom", OLD_DIGEST)}
        for name, text in files.items():
            path = seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.git("add", ".", cwd=seed)
        self.git("commit", "-q", "-m", "seed", cwd=seed)
        self.git("remote", "add", "origin", self.url, cwd=seed)
        self.git("push", "-q", "origin", "main", cwd=seed)
        self.git("clone", "-q", self.url, str(self.rival))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run([REAL_GIT, *args], cwd=cwd, env=self.env, capture_output=True, text=True,
                              encoding="utf-8", timeout=60, check=True)
        return done.stdout

    def remote(self, *args: str) -> str:
        return self.git("--git-dir", str(self.bare), *args)

    def remote_count(self) -> int:
        return int(self.remote("rev-list", "--count", "main"))

    def remote_file(self, name: str) -> str:
        return self.remote("show", f"main:{name}")

    def last_commit_numstat(self) -> str:
        return self.remote("diff-tree", "--no-commit-id", "--numstat", "-r", "main")

    def run(self, rival: str = "", command: str | None = "update", **overrides: str | None) -> GitopsRun:
        self.git_log.write_text("", encoding="utf-8")
        self.ssh_log.write_text("", encoding="utf-8")
        self.rival_state.unlink(missing_ok=True)
        inputs = {"IM_IMAGE_DIGEST": IMAGE_REF, "IM_SHA_FULL": FULL_SHA, "GITOPS_URL": self.url,
                  "GITOPS_OVERLAY": OVERLAY_VENOM, "GITOPS_SSH_KEY": self.key.as_posix(),
                  "GITOPS_KNOWN_HOSTS": KNOWN_HOSTS, "IM_MIRROR_APPROVED_BY": APPROVER}
        inputs.update(overrides)
        env = dict(self.env)
        env["PATH"] = str(self.stubs) + os.pathsep + env.get("PATH", "")
        env.update({"REAL_GIT": Path(REAL_GIT).as_posix(), "GIT_CALL_LOG": self.git_log.as_posix(),
                    "GIT_SSH_LOG": self.ssh_log.as_posix(),
                    "RIVAL_MODE": rival, "RIVAL_STATE": self.rival_state.as_posix(),
                    "RIVAL_DIR": self.rival.as_posix(), "TMPDIR": self.scratch.as_posix()})
        for name, value in inputs.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        argv = [BASH, GITOPS_STEPS.as_posix()] + ([command] if command is not None else [])
        completed = subprocess.run(argv, cwd=self.tmp, env=env, capture_output=True, text=True, encoding="utf-8",
                                   timeout=120)
        return GitopsRun(completed, read_log(self.git_log), read_log(self.ssh_log))


@unittest.skipUnless(BASH and REAL_GIT, "bash or git is not available")
# Scenario US1-AS7: stage scripts, one commit per new digest, none for the same digest (pinned existing tests).
class GitopsSteps(unittest.TestCase):
    """T1 to T5 of task #3691. Covers AC-4, FR-10, NFR-01, NFR-07, NFR-12."""

    def fixture(self, venom: str | None = None) -> GitopsFixture:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        return GitopsFixture(tmp, venom)

    def assert_no_leak(self, result: GitopsRun) -> None:
        for stream in (result.stdout, result.stderr, result.git_log):
            self.assertNotIn(KEY_CONTENT, stream)
        for stream in (result.stdout, result.stderr):
            self.assertNotIn(KNOWN_HOSTS, stream)

    def bash_exists(self, path: str) -> bool:
        return subprocess.run([BASH, "-c", 'test -e "$1"', "_", path]).returncode == 0

    def test_T1_a_new_digest_adds_exactly_one_commit_and_exits_zero(self):
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)

    def test_T1_exactly_one_line_changes_in_the_venom_overlay(self):
        fx = self.fixture()
        fx.run()
        self.assertEqual(fx.last_commit_numstat(), f"1\t1\t{OVERLAY_VENOM}\n")
        self.assertEqual(fx.remote_file(OVERLAY_VENOM), overlay_text("venom", NEW_DIGEST))

    def test_T1_the_other_apps_digest_line_is_untouched(self):
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        self.assertEqual(fx.remote_file(OVERLAY_OTHER), overlay_text("other", OTHER_DIGEST))

    def test_T1_prints_the_previous_digest_and_the_updated_one(self):
        fx = self.fixture()
        result = fx.run()
        self.assertIn(f"gitops-steps: previous digest {OLD_DIGEST}", result.stdout.splitlines())
        self.assertIn(f"gitops-steps: updated to {NEW_DIGEST}", result.stdout.splitlines())

    def test_T1_commit_message_and_author_are_the_fixed_ones(self):
        fx = self.fixture()
        fx.run()
        self.assertEqual(fx.remote("log", "-1", "--format=%s"),
                         f"site: set venom digest to 0123456789ab (source {SHORT_SHA})\n")
        self.assertEqual(fx.remote("log", "-1", "--format=%an <%ae>"),
                         "venom publish job <venom-publish@example.invalid>\n")

    def test_T1_the_branch_defaults_to_main(self):
        fx = self.fixture()
        result = fx.run(GITOPS_BRANCH=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        self.assertEqual(len(result.calls(r"--branch main\b")), 1)

    def test_T1_the_digest_value_written_is_the_part_after_the_at_sign(self):
        fx = self.fixture()
        result = fx.run(IM_IMAGE_DIGEST=f"{REGISTRY}:5000/team/venom-site@{NEW_DIGEST}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_file(OVERLAY_VENOM), overlay_text("venom", NEW_DIGEST))
        self.assertIn(f"gitops-steps: updated to {NEW_DIGEST}", result.stdout.splitlines())

    def test_T1_without_a_key_the_ssh_command_is_not_set_and_the_known_hosts_are_not_required(self):
        fx = self.fixture()
        result = fx.run(GITOPS_SSH_KEY=None, GITOPS_KNOWN_HOSTS=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        self.assertEqual(result.ssh_calls, [])

    def test_T1_clones_shallow_the_requested_branch_and_pushes_to_it(self):
        fx = self.fixture()
        result = fx.run(GITOPS_BRANCH="main")
        self.assertEqual(result.returncode, 0, result.stderr)
        clones = result.calls(r"\bclone\b")
        self.assertEqual(len(clones), 1)
        self.assertIn("--depth 1", clones[0])
        self.assertIn("--branch main", clones[0])
        self.assertEqual(len(result.calls(r"\bpush origin HEAD:main$")), 1)

    def test_T4_a_missing_or_empty_required_input_stops_before_any_git_command_and_names_the_variable(self):
        fx = self.fixture()
        for name in ("IM_IMAGE_DIGEST", "IM_SHA_FULL", "GITOPS_URL", "GITOPS_OVERLAY", "GITOPS_KNOWN_HOSTS",
                     "IM_MIRROR_APPROVED_BY"):
            for value in (None, ""):
                with self.subTest(missing=name, value=value):
                    result = fx.run(**{name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.git_calls, [])
                    self.assertEqual(list(fx.scratch.iterdir()), [])
                    self.assertEqual(fx.remote_count(), 1)
                    self.assert_no_leak(result)

    def test_T4_an_unknown_command_or_none_prints_usage_and_exits_two_before_any_git_command(self):
        fx = self.fixture()
        for command in (None, "push", "build", "Update", ""):
            with self.subTest(command=command):
                result = fx.run(command=command)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("usage", result.stderr.lower())
                self.assertEqual(result.git_calls, [])
                self.assertEqual(list(fx.scratch.iterdir()), [])
                self.assertEqual(fx.remote_count(), 1)

    def test_T2_the_same_digest_again_makes_no_commit_and_says_unchanged(self):
        fx = self.fixture(overlay_text("venom", NEW_DIGEST))
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gitops-steps: unchanged", result.stdout.splitlines())
        self.assertEqual(fx.remote_count(), 1)

    def test_T2_the_same_digest_runs_neither_commit_nor_push(self):
        fx = self.fixture(overlay_text("venom", NEW_DIGEST))
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.calls(r"\bclone\b")), 1)
        self.assertEqual(result.calls(r"\bcommit\b"), [])
        self.assertEqual(result.calls(r"\bpush\b"), [])

    def test_T3_a_malformed_image_reference_is_rejected_before_any_git_command(self):
        fx = self.fixture()
        good = "sha256:" + "e" * 64
        bad = {
            "short": f"{IMAGE}@sha256:" + "e" * 63,
            "long": f"{IMAGE}@sha256:" + "e" * 65,
            "no repository": good,
            "empty repository": "@" + good,
            "no at sign": f"{IMAGE}:{good}",
            "other algorithm": f"{IMAGE}@md5:" + "e" * 64,
            "upper case": f"{IMAGE}@sha256:" + "E" * 64,
            "not hex": f"{IMAGE}@sha256:" + "g" * 64,
            "inner space": f"{IMAGE}@sha256:" + "e" * 32 + " " + "e" * 32,
            "trailing space": f"{IMAGE}@{good} ",
            "two at signs": f"{IMAGE}@{IMAGE}@{good}",
            "repository with a space": f"regis try/venom@{good}",
            "repository with a semicolon": f"{REGISTRY}/venom;touch x@{good}",
            "repository with a dollar": f"{REGISTRY}/$HOME@{good}",
            "repository starting with a dash": f"-{IMAGE}@{good}",
            "value starting with two dashes": f"--upload-pack=x@{good}",
            "newline in the repository": f"{REGISTRY}/venom\n@{good}",
        }
        for name, reference in bad.items():
            with self.subTest(case=name):
                result = fx.run(IM_IMAGE_DIGEST=reference)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("IM_IMAGE_DIGEST", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(list(fx.scratch.iterdir()), [])
                self.assertEqual(fx.remote_count(), 1)

    def test_T3_a_malformed_commit_label_is_rejected_before_any_git_command(self):
        fx = self.fixture()
        bad = {
            "too short": "abc123",
            "too long": "a" * 41,
            "upper case": "ABC1234",
            "not hex": "abc123g",
            "space": "abc1234 def",
            "dash": "-abc1234",
            "semicolon": "abc1234;x",
        }
        for name, sha in bad.items():
            with self.subTest(case=name):
                result = fx.run(IM_SHA_FULL=sha)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("IM_SHA_FULL", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(list(fx.scratch.iterdir()), [])
                self.assertEqual(fx.remote_count(), 1)

    def test_T4_a_venom_overlay_without_a_digest_line_pushes_nothing(self):
        fx = self.fixture(overlay_text("venom"))
        result = fx.run()
        self.assertEqual(len(result.calls(r"\bclone\b")), 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest", result.stderr.lower())
        self.assertEqual(result.calls(r"\bpush\b"), [])
        self.assertEqual(fx.remote_count(), 1)
        self.assert_no_leak(result)

    def test_T4_a_venom_overlay_with_two_digest_lines_pushes_nothing(self):
        fx = self.fixture(overlay_text("venom", OLD_DIGEST, OTHER_DIGEST))
        result = fx.run()
        self.assertEqual(len(result.calls(r"\bclone\b")), 1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest", result.stderr.lower())
        self.assertEqual(result.calls(r"\bpush\b"), [])
        self.assertEqual(fx.remote_count(), 1)
        self.assert_no_leak(result)

    # Scenario US1-AS8: credentials stay out of the logs (pinned existing test).
    def test_T4_the_deploy_key_content_never_appears_on_success(self):
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        self.assert_no_leak(result)

    def commit_body(self, fx: GitopsFixture) -> str:
        return fx.remote("log", "-1", "--format=%B")

    def test_the_commit_body_holds_source_digest_approver_and_a_utc_time_in_that_order(self):
        """Task T006, US1-AS14, NFR-007."""
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        lines = fx.remote("log", "-1", "--format=%b").strip("\n").splitlines()
        self.assertEqual(lines[:3], [f"Source: {FULL_SHA}", f"Image digest: {NEW_DIGEST}",
                                     f"Approved by: {APPROVER}"])
        self.assertEqual(len(lines), 4, lines)
        self.assertRegex(lines[3], r"^Recorded: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_a_missing_or_unsafe_approver_is_refused_before_any_git_command(self):
        """Task T006, US1-AS14, NFR-007: the id character set is ^[A-Za-z0-9._@-]{1,80}$."""
        fx = self.fixture()
        for value in (None, "", "two words", "semi;colon", "$(id)", "back`tick`", "a" * 81):
            with self.subTest(value=value):
                result = fx.run(IM_MIRROR_APPROVED_BY=value)
                self.assertNotEqual(result.returncode, 0, f"the approver {value!r} was accepted")
                self.assertIn("IM_MIRROR_APPROVED_BY", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(list(fx.scratch.iterdir()), [])
                self.assertEqual(fx.remote_count(), 1)

    def test_an_unchanged_digest_leaves_the_remote_log_and_body_unchanged(self):
        """Task T006, US1-AS14 (pinned characterisation): no commit means no new record."""
        fx = self.fixture(overlay_text("venom", NEW_DIGEST))
        before = fx.remote("log", "--format=%H %B")
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote("log", "--format=%H %B"), before)
        self.assertNotIn("Approved by", before + self.commit_body(fx))

    def test_the_deploy_key_content_is_not_in_the_commit_body(self):
        """Task T006, NFR-001 (pinned characterisation)."""
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fx.remote_count(), 2)
        body = self.commit_body(fx)
        self.assertNotIn(KEY_CONTENT, body)
        self.assertNotIn(KNOWN_HOSTS, body)

    def test_T3_an_unsafe_overlay_path_is_rejected_before_any_git_command(self):
        fx = self.fixture()
        bad = {
            "absolute": "/tmp/overlay.yaml",
            "drive letter": "C:/x/overlay.yaml",
            "parent": "../escape.yaml",
            "inner parent": "apps/../../escape.yaml",
            "space": "apps/ven om/overlay.yaml",
            "semicolon": "apps/venom;touch x/overlay.yaml",
            "dollar": "apps/$HOME/overlay.yaml",
            "backtick": "apps/`id`/overlay.yaml",
        }
        for name, overlay in bad.items():
            with self.subTest(case=name):
                result = fx.run(GITOPS_OVERLAY=overlay)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("GITOPS_OVERLAY", result.stderr)
                self.assertEqual(result.git_calls, [])
                self.assertEqual(list(fx.scratch.iterdir()), [])
                self.assertFalse((fx.tmp.parent / "escape.yaml").exists())

    def test_T3_an_unsafe_url_or_branch_is_rejected_before_any_git_command(self):
        fx = self.fixture()
        bad = {
            "GITOPS_URL": ("file:///x y", "file:///x;y", "file:///x`y`", "file:///x$y", "file:///x|y"),
            "GITOPS_BRANCH": ("main;x", "ma in", "$(x)", "main&x"),
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(variable=name, value=value):
                    result = fx.run(**{name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.git_calls, [])
                    self.assertEqual(list(fx.scratch.iterdir()), [])

    def test_T5_a_rejected_push_rebases_once_and_pushes_again(self):
        fx = self.fixture()
        result = fx.run(rival="first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.calls(r"\bpull --rebase origin main\b")), 1)
        self.assertEqual(len(result.calls(r"\bpush\b")), 2)
        self.assertEqual(fx.remote_count(), 3)

    def test_T5_after_the_rebase_still_one_line_changes_and_the_rival_commit_stays(self):
        fx = self.fixture()
        fx.run(rival="first")
        self.assertEqual(fx.last_commit_numstat(), f"1\t1\t{OVERLAY_VENOM}\n")
        self.assertEqual(fx.remote_file(OVERLAY_VENOM), overlay_text("venom", NEW_DIGEST))
        self.assertEqual(fx.remote_file(OVERLAY_OTHER), overlay_text("other", "sha256:" + "0" * 63 + "1"))

    def test_T5_a_second_rejection_fails_with_a_clear_message(self):
        fx = self.fixture()
        result = fx.run(rival="every")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("push", result.stderr.lower())
        self.assertEqual(len(result.calls(r"\bpull --rebase origin main\b")), 1)
        self.assertEqual(len(result.calls(r"\bpush\b")), 2)
        self.assertNotIn(NEW_DIGEST, fx.remote_file(OVERLAY_VENOM))
        self.assert_no_leak(result)

    def test_T5_the_ssh_command_uses_a_private_known_hosts_file_and_strict_checking(self):
        fx = self.fixture()
        result = fx.run(rival="first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(len(result.ssh_calls), 3)
        paths = set()
        for command, path, exists, content in result.ssh_calls:
            self.assertIn(f"-i {fx.key.as_posix()}", command)
            self.assertIn("-o IdentitiesOnly=yes", command)
            self.assertIn("-o StrictHostKeyChecking=yes", command)
            self.assertNotIn("accept-new", command)
            self.assertNotEqual(path, "")
            self.assertEqual(exists, "1")
            self.assertEqual(content, KNOWN_HOSTS + "|")
            paths.add(path)
        self.assertEqual(len(paths), 1)
        self.assertNotEqual(paths.pop(), fx.key.as_posix())
        self.assert_no_leak(result)

    def test_T5_the_known_hosts_file_is_removed_on_success_and_on_failure(self):
        for label, venom, rival in (("success", None, ""), ("failed push", None, "every"),
                                    ("no digest line", overlay_text("venom"), "")):
            with self.subTest(exit=label):
                fx = self.fixture(venom)
                result = fx.run(rival=rival)
                self.assertTrue(result.ssh_calls, label)
                self.assertEqual(list(fx.scratch.iterdir()), [])
                for _command, path, _exists, _content in result.ssh_calls:
                    self.assertFalse(self.bash_exists(path), path)
                self.assert_no_leak(result)

    def test_T5_the_ssh_command_is_not_set_without_a_key(self):
        fx = self.fixture()
        result = fx.run(GITOPS_SSH_KEY=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.ssh_calls, [])

    def test_review_a_value_starting_with_a_dash_is_rejected_before_any_git_command(self):
        fx = self.fixture()
        bad = {
            "GITOPS_URL": ("--upload-pack=true", "-x", "-"),
            "GITOPS_BRANCH": ("-main", "--force", "-"),
            "GITOPS_OVERLAY": ("-apps/venom/overlay.yaml", "--overlay.yaml"),
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(variable=name, value=value):
                    result = fx.run(**{name: value})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.git_calls, [])
                    self.assertEqual(list(fx.scratch.iterdir()), [])
                    self.assertEqual(fx.remote_count(), 1)

    def test_review_the_clone_puts_double_dash_before_the_url(self):
        fx = self.fixture()
        result = fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        clones = result.calls(r"\bclone\b")
        self.assertEqual(len(clones), 1)
        self.assertRegex(clones[0], rf"^clone .* -- {re.escape(fx.url)} \S+$")
        self.assertLess(clones[0].index("--branch"), clones[0].index(" -- "))

    def link_into_remote(self, fx: GitopsFixture, kind: str) -> Path:
        outside_dir = fx.tmp / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "overlay.yaml"
        outside_file.write_text(overlay_text("venom", OLD_DIGEST), encoding="utf-8", newline="\n")
        if kind == "file":
            fx.git("rm", "-q", OVERLAY_VENOM, cwd=fx.rival)
            link, target, is_dir = fx.rival / OVERLAY_VENOM, outside_file, False
        else:
            fx.git("rm", "-q", "-r", "apps/venom", cwd=fx.rival)
            link, target, is_dir = fx.rival / "apps" / "venom", outside_dir, True
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=is_dir)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"this platform cannot create symlinks: {error}")
        rel = link.relative_to(fx.rival).as_posix()
        fx.git("-c", "core.symlinks=true", "add", rel, cwd=fx.rival)
        fx.git("commit", "-q", "-m", f"{kind} symlink", cwd=fx.rival)
        fx.git("push", "-q", "origin", "HEAD:main", cwd=fx.rival)
        return outside_file

    def assert_symlink_refused(self, kind: str) -> None:
        fx = self.fixture()
        outside = self.link_into_remote(fx, kind)
        before = outside.read_text(encoding="utf-8")
        count = fx.remote_count()
        result = fx.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr.lower(), r"symlink|outside")
        self.assertEqual(result.calls(r"\bcommit\b"), [])
        self.assertEqual(result.calls(r"\bpush\b"), [])
        self.assertEqual(fx.remote_count(), count)
        self.assertEqual(outside.read_text(encoding="utf-8"), before)
        self.assert_no_leak(result)

    def test_review_an_overlay_file_that_is_a_symlink_is_refused_and_the_target_untouched(self):
        self.assert_symlink_refused("file")

    def test_review_a_directory_symlink_on_the_overlay_path_is_refused_and_the_target_untouched(self):
        self.assert_symlink_refused("directory")


if __name__ == "__main__":
    unittest.main()
