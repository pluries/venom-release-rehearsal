#!/usr/bin/env python3
"""Shared fixture of the release tests (feature 011, task T003). A helper module, not a test file.
It gives the test_site_release_*.py files: a FakeGitHub (standard-library http.server on 127.0.0.1 that keeps refs,
annotated tags, releases, assets and drafts, records every request and can inject failures), a builder of a
temporary tree that holds copies of jenkins/release-*.sh and release-*.txt next to a local bare "public repository",
stub make, go, docker, openssl and a recording git wrapper first on PATH (each records argv, directory and environment
before it does anything), Tree.run() that returns status, output and the recorded stub calls, and canary(), a fake
credential assembled at run time so no whole value sits in a file.
Signing (D-059: Certum signs SHA256SUMS as a detached CMS signature, minisign is gone): the openssl stub knows the calls
of the release scripts (`cms -verify`, `x509 -fingerprint -sha256`, `ts -query`, `ts -verify`). A fake SHA256SUMS.p7s
holds `cms <sha256 of SHA256SUMS>` and a fake SHA256SUMS.p7s.tsr holds `tsr <sha256 of the p7s>`, so a changed byte
fails the stub's verify the way it fails the real one (fake_cms, fake_tsr). The signer fingerprint the stub prints is
STUB_SIGNER_SHA256 (default SIGNER_SHA256); Tree.write_certificate() writes the pinned jenkins/release-certificate.json.
FakeGitHub also plays the timestamp server at /tsa.
Offline, standard library only. Never holds a real credential.
Stub behaviour comes from a config file next to the stubs (not from the environment), so a script that runs a tool
with a cleaned environment still gets the planted behaviour.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

TOOLS = ("bash", "git", "node", "sha256sum", "curl")
PRIVATE_PATHS = ("jenkins/release-lib.sh", "jenkins/release-steps.sh", "jenkins/release-github.sh",
                 "jenkins/release-leak-check.sh", "jenkins/release-leak-patterns.txt", "docs", ".claude",
                 "cmd/venom/Makefile")
MISSING = [tool for tool in TOOLS if not shutil.which(tool)] + [p for p in PRIVATE_PATHS if not (ROOT / p).exists()]
SKIP_REASON = "release tests need " + ", ".join(TOOLS) + " on PATH and the private paths " + ", ".join(PRIVATE_PATHS) \
    + "; missing: " + ", ".join(MISSING)
requires_release = unittest.skipIf(bool(MISSING), SKIP_REASON)
BASH = shutil.which("bash")
IS_MSYS = sys.platform == "win32"
MSYS_REASON = "process-group kill is unreliable on MSYS"

EXPECTED_NAMES = ["venom.linux-amd64", "venom.linux-arm", "venom.linux-386", "venom.linux-arm64",
                  "venom.linux-ppc64le", "venom.darwin-amd64", "venom.darwin-arm64", "venom.windows-amd64.exe",
                  "venom.openbsd-amd64", "venom.freebsd-amd64"]
TAG = "v1.4.0-pluries.2"
JOB = "venom/venom-release"
REPOSITORY = "pluries-example/venom-example"
SECRET_NAME = re.compile(r"TOKEN|KEY|PASSWORD|SECRET|^RELEASE_", re.IGNORECASE)
GIT_CONFIG = "[user]\n\tname = fixture user\n\temail = fixture@example.invalid\n[init]\n\tdefaultBranch = master\n"

PRELUDE = """\
#!/usr/bin/env bash
stub_dir="$(cd "$(dirname "$0")" && pwd)"
name="$(basename "$0")"
mkdir -p "$stub_dir/log"
{ printf 'CALL'; printf '\\t%s' "$@"; printf '\\n'; printf 'PWD\\t%s\\n' "$PWD"; env | sed 's/^/ENV\\t/'; echo END; } >> "$stub_dir/log/$name.calls"
. "$stub_dir/config.env"
"""

MAKE_STUB = PRELUDE + """\
if [ "${1:-}" = "-C" ]; then cd "$2"; shift 2; fi
count_file="$stub_dir/log/make.count"
n=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$count_file"
if [ -n "${STUB_MAKE_FAIL:-}" ]; then echo "make refused" >&2; exit 1; fi
tag=""
for a in "$@"; do case "$a" in GIT_DESCRIBE=*) tag="${a#GIT_DESCRIBE=}";; esac; done
native="venom.${STUB_GOOS:-linux}-${STUB_GOARCH:-amd64}"
mkdir -p dist
for f in $STUB_NAMES; do
  body="binary $f $tag"
  if [ "$f" = "${STUB_MAKE_DIFFER:-}" ] && [ "$n" -gt 1 ]; then body="$body call$n"; fi
  if [ "$f" = "${STUB_MAKE_EMBED_PWD:-}" ]; then body="$body $PWD"; fi
  if [ "$f" = "$native" ]; then
    printf '#!/bin/sh\\necho "$0 $*" >> "%s/log/native.calls"\\necho "Version venom: %s"\\n# %s\\n' \\
      "$stub_dir" "${STUB_NATIVE_VERSION:-$tag}" "$body" > "dist/$f"
    chmod +x "dist/$f"
  else
    printf '%s\\n' "$body" > "dist/$f"
  fi
done
"""

GO_STUB = PRELUDE + """\
build_info() {
  local file="$1" base ver tag
  base="$(basename "$file")"; ver="${STUB_GO_VERSION:-1.27.3}"; tag="${STUB_GO_TAG:-}"
  if [ "$base" = "${STUB_GO_WRONG_TAG_FILE:-}" ]; then tag="v0.0.0-wrong"; fi
  printf '%s: go%s\\n\\tpath\\tgithub.com/ovh/venom/cmd/venom\\n\\tbuild\\t-buildmode=exe\\n' "$file" "$ver"
  if [ "$base" != "${STUB_GO_BAD_FILE:-}" ] || [ "${STUB_GO_BAD_KIND:-}" != "trimpath" ]; then
    printf '\\tbuild\\t-trimpath=true\\n'
  fi
  printf '\\tbuild\\t-ldflags="-X github.com/ovh/venom.Version=%s"\\n' "$tag"
  if [ "$base" = "${STUB_GO_BAD_FILE:-}" ] && [ "${STUB_GO_BAD_KIND:-}" = "cgo" ]; then
    printf '\\tbuild\\tCGO_ENABLED=1\\n'
  else
    printf '\\tbuild\\tCGO_ENABLED=0\\n'
  fi
  if [ "$base" = "${STUB_GO_BAD_FILE:-}" ] && [ "${STUB_GO_BAD_KIND:-}" = "vcs" ]; then
    printf '\\tbuild\\tvcs.time=2026-01-01T00:00:00Z\\n'
  fi
}
case "${1:-}" in
  version)
    if [ "${2:-}" = "-m" ]; then
      shift 2
      for file in "$@"; do build_info "$file"; done
    else
      echo "go version go${STUB_GO_VERSION:-1.27.3} linux/amd64"
    fi ;;
  env)
    shift
    for key in "$@"; do
      case "$key" in GOOS) echo "${STUB_GOOS:-linux}";; GOARCH) echo "${STUB_GOARCH:-amd64}";; *) echo "";; esac
    done ;;
  mod)
    if [ -n "${STUB_GO_VERIFY_FAIL:-}" ]; then echo "go: modules not verified" >&2; exit 1; fi
    if [ -n "${STUB_GO_TOUCH_GOSUM:-}" ]; then
      d="$PWD"
      while [ ! -f "$d/go.sum" ] && [ "$d" != "/" ]; do d="$(dirname "$d")"; done
      echo "example.test/module v0.0.0 h1:changed" >> "$d/go.sum"
    fi
    echo "all modules verified" ;;
esac
"""

DOCKER_STUB = PRELUDE + """\
if [ "${STUB_DOCKER_FAIL:-}" = "${1:-}" ]; then echo "docker $1 refused" >&2; exit 1; fi
case "${1:-}" in
  login) cat > "$stub_dir/log/docker.login.stdin" ;;
  manifest) printf '%s\\n' "${STUB_DOCKER_MANIFEST_OUT:-no such manifest}" >&2; exit "${STUB_DOCKER_MANIFEST_RC:-1}" ;;
esac
"""

OPENSSL_STUB = PRELUDE + """\
digest_of() { sha256sum "$1" | cut -d' ' -f1; }
sub="${1:-}"
[ $# -eq 0 ] || shift
case "$sub" in
  cms)
    in=""; content=""; signer=""
    while [ $# -gt 0 ]; do
      case "$1" in -in) in="$2"; shift;; -content) content="$2"; shift;; -signer) signer="$2"; shift;; -out|-inform|-purpose) shift;; esac
      shift
    done
    if [ -n "${STUB_CMS_FAIL:-}" ]; then echo "CMS Verification failure" >&2; exit 4; fi
    [ -s "$in" ] || { echo "Error reading CMS Content Info" >&2; exit 1; }
    [ "$(cat "$in")" = "cms $(digest_of "$content")" ] || { echo "CMS Verification failure" >&2; exit 4; }
    printf 'fixture signer certificate %s\\n' "$STUB_SIGNER_SHA256" > "$signer"
    echo "CMS Verification successful" >&2 ;;
  x509)
    in=""
    while [ $# -gt 0 ]; do
      case "$1" in -in) in="$2"; shift;; esac
      shift
    done
    [ -f "$in" ] || { echo "cannot read $in" >&2; exit 1; }
    hex="$(awk '{print $NF}' "$in" | head -n 1)"
    printf 'sha256 Fingerprint=%s\\n' "$(printf '%s' "$hex" | tr a-f A-F | sed 's/../&:/g; s/:$//')" ;;
  ts)
    mode=""; data=""; out=""; in=""
    while [ $# -gt 0 ]; do
      case "$1" in -query) mode=query;; -verify) mode=verify;; -data) data="$2"; shift;; -out) out="$2"; shift;; -in) in="$2"; shift;; -CAfile) shift;; esac
      shift
    done
    if [ "$mode" = query ]; then
      if [ -n "${STUB_TS_QUERY_FAIL:-}" ]; then echo "cannot make the request" >&2; exit 1; fi
      printf 'tsq %s' "$(digest_of "$data")" > "$out"
    else
      if [ -n "${STUB_TS_VERIFY_FAIL:-}" ]; then echo "Verification: FAILED" >&2; exit 1; fi
      [ "$(cat "$in")" = "tsr $(digest_of "$data")" ] || { echo "Verification: FAILED" >&2; exit 1; }
      echo "Verification: OK"
    fi ;;
esac
"""

TSA_CURL_STUB = PRELUDE + """\
out=""; data=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift;; --data-binary) data="${2#@}"; shift;; -H|--max-time) shift;; esac
  shift
done
if [ -n "${STUB_TSA_FAIL:-}" ]; then echo "curl: (22) refused" >&2; exit 22; fi
printf 'tsr %s' "$(cut -c5- "$data")" > "$out"
"""

WRAPPER = PRELUDE + """\
real="$(type -ap TOOL | sed -n 2p)"
exec "$real" "$@"
"""

GIT_WRAPPER = WRAPPER.replace("TOOL", "git")
CURL_WRAPPER = WRAPPER.replace("TOOL", "curl")
TAR_WRAPPER = WRAPPER.replace("TOOL", "tar")


def canary(*parts: str) -> str:
    """A fake credential joined at run time, so no whole value sits in any file."""
    return "-".join(parts or ("canary", "token", "not", "real", "4711"))


def clean_env() -> dict:
    """The parent environment without anything a release script must never see."""
    return {k: v for k, v in os.environ.items()
            if not SECRET_NAME.search(k) and not k.startswith(("STUB_", "GIT_"))}


class Call:
    """One recorded call of a stub: argv, working directory and environment."""

    def __init__(self, argv: list, pwd: str, env: dict):
        self.argv, self.pwd, self.env = argv, pwd, env

    def secret_names(self) -> list:
        return [name for name in self.env if SECRET_NAME.search(name)]


def parse_calls(text: str) -> list:
    calls, argv, pwd, env = [], [], "", {}
    for line in text.splitlines():
        kind, _, rest = line.partition("\t")
        if kind == "CALL":
            argv, pwd, env = rest.split("\t") if rest or line.count("\t") else [], "", {}
        elif kind == "PWD":
            pwd = rest
        elif kind == "ENV":
            name, _, value = rest.partition("=")
            env[name] = value
        elif line == "END":
            calls.append(Call(argv, pwd, env))
    return calls


class Result:
    def __init__(self, completed: subprocess.CompletedProcess, log_dir: Path):
        self.returncode, self.stdout, self.stderr = completed.returncode, completed.stdout, completed.stderr
        self.log_dir = log_dir

    def calls(self, stub: str) -> list:
        path = self.log_dir / f"{stub}.calls"
        return parse_calls(path.read_text(encoding="utf-8")) if path.is_file() else []

    def output(self) -> str:
        return self.stdout + self.stderr


SIGNER_SHA256 = hashlib.sha256(b"fixture certificate, not a certificate").hexdigest()
CERT_SUBJECT = "Fixture Publisher"


def fake_cms(sums: bytes) -> bytes:
    """What the openssl stub accepts as the detached signature of these SHA256SUMS bytes."""
    return f"cms {hashlib.sha256(sums).hexdigest()}".encode()


def fake_tsr(p7s: bytes) -> bytes:
    """What the openssl stub and the fake timestamp server accept as the timestamp token of these p7s bytes."""
    return f"tsr {hashlib.sha256(p7s).hexdigest()}".encode()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def git(env: dict, *args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, check=True)
    return done.stdout.strip()


def write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


class Tree:
    """A temporary private tree with the scripts, a bare public repository, stubs and a work root."""

    def __init__(self, tmp: Path, **config):
        self.tmp = tmp
        self.root = tmp / "tree"
        self.stubs = tmp / "stubs"
        self.public = tmp / "public" / (REPOSITORY + ".git")
        self.work_root = tmp / "work"
        self.log_dir = self.stubs / "log"
        self.git_env = {**clean_env(), "GIT_CONFIG_GLOBAL": (tmp / "gitconfig").as_posix(),
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        write(tmp / "gitconfig", GIT_CONFIG)
        self.settings = {"STUB_NAMES": " ".join(EXPECTED_NAMES), "STUB_GO_TAG": TAG,
                         "STUB_SIGNER_SHA256": SIGNER_SHA256}
        self.settings.update(config)
        self.base_env = {"RELEASE_PUBLIC_URL": self.public.as_posix(), "RELEASE_WORK_ROOT": self.work_root.as_posix(),
                         "JOB_NAME": JOB, "BUILD_NUMBER": "1", "RELEASE_TAG": TAG, "GIT_CONFIG_GLOBAL":
                         (tmp / "gitconfig").as_posix(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        self.copy_scripts()
        self.make_stubs()
        self.make_public()
        self.base_env["RELEASE_SHA"] = self.sha_first

    def copy_scripts(self) -> None:
        jenkins = self.root / "jenkins"
        jenkins.mkdir(parents=True)
        for pattern in ("release-*.sh", "release-*.txt"):
            for source in (ROOT / "jenkins").glob(pattern):
                shutil.copy(source, jenkins / source.name)
        exceptions = jenkins / "release-repro-exceptions.txt"
        if not exceptions.exists():
            write(exceptions, "# <file> <reason> <approved-by> <date>\n")

    def configure(self, **config) -> None:
        self.settings.update(config)
        text = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in self.settings.items())
        write(self.stubs / "config.env", text)

    def make_stubs(self) -> None:
        self.stubs.mkdir()
        for name, text in (("make", MAKE_STUB), ("go", GO_STUB), ("docker", DOCKER_STUB),
                           ("openssl", OPENSSL_STUB), ("git", GIT_WRAPPER), ("curl", CURL_WRAPPER),
                           ("tar", TAR_WRAPPER)):
            write(self.stubs / name, text, executable=True)
        self.configure()

    def make_public(self) -> None:
        seed = self.tmp / "public-seed"
        git(self.git_env, "init", "--bare", "-b", "master", str(self.public))
        git(self.git_env, "init", "-b", "master", str(seed))
        files = {"go.mod": "module github.com/ovh/venom\n\ngo 1.27.3\n", "go.sum": "example.test/module v0.0.0 h1:x\n",
                 "cmd/venom/Makefile": "TARGET_NAME = venom\n", "LICENSE": "licence text of the fixture\n",
                 "NOTICE": "notice text of the fixture\n", "Dockerfile": "FROM scratch\n"}
        for name, text in files.items():
            write(seed / name, text)
        git(self.git_env, "add", ".", cwd=seed)
        git(self.git_env, "commit", "-q", "-m", "first public commit", cwd=seed)
        self.sha_first = git(self.git_env, "rev-parse", "HEAD", cwd=seed)
        write(seed / "README.md", "the public fork\n")
        git(self.git_env, "add", ".", cwd=seed)
        git(self.git_env, "commit", "-q", "-m", "second public commit, release v8.0.0-pluries.5", cwd=seed)
        self.sha_tip = git(self.git_env, "rev-parse", "HEAD", cwd=seed)
        git(self.git_env, "push", "-q", str(self.public), "master", cwd=seed)
        self.seed = seed

    def public_tag(self, tag: str, annotated: bool = False, commit: str | None = None) -> None:
        args = ["tag", "-a", "-m", f"notes of {tag}", tag] if annotated else ["tag", tag]
        git(self.git_env, *args, *([commit] if commit else []), cwd=self.seed)
        git(self.git_env, "push", "-q", str(self.public), f"refs/tags/{tag}", cwd=self.seed)

    def write_private(self, relative: str, text: str) -> None:
        write(self.root / relative, text)

    def work_dir(self, build: str = "1") -> Path:
        found = sorted(self.work_root.glob(f"*/{build}"))
        if len(found) != 1:
            raise AssertionError(f"expected one work directory for build {build} under {self.work_root}: {found}")
        return found[0]

    def flags(self, build: str = "1") -> list:
        return sorted(self.work_root.glob(f"*/{build}/claim.flag"))

    def make_work(self, build: str = "1") -> Path:
        """The work directory of a build, made the way work_dir() of release-lib.sh names it."""
        path = self.work_root / JOB.replace("/", "-") / build
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare_release_files(self, build: str = "1", signature: bool = True) -> dict:
        """dist/ with the ten files, SHA256SUMS and (stub format) SHA256SUMS.p7s and SHA256SUMS.p7s.tsr, plus src/LICENSE
        and src/NOTICE, in the work directory. Returns every release asset, name to bytes: the fifteen files a release
        carries. Without signature the p7s and its timestamp are not written to dist/ (the sums are not signed yet)."""
        work = self.make_work(build)
        assets = {name: f"binary {name} {TAG}\n".encode() for name in EXPECTED_NAMES}
        sums = "".join(f"{hashlib.sha256(assets[n]).hexdigest()}  {n}\n" for n in sorted(EXPECTED_NAMES))
        assets["SHA256SUMS"] = sums.encode()
        assets["SHA256SUMS.p7s"] = fake_cms(assets["SHA256SUMS"])
        assets["SHA256SUMS.p7s.tsr"] = fake_tsr(assets["SHA256SUMS.p7s"])
        assets["LICENSE"] = b"licence text of the fixture\n"
        assets["NOTICE"] = b"notice text of the fixture\n"
        for name, data in assets.items():
            if name in ("LICENSE", "NOTICE") or (name.startswith("SHA256SUMS.p7s") and not signature):
                continue
            write_bytes(work / "dist" / name, data)
        write_bytes(work / "src" / "LICENSE", assets["LICENSE"])
        write_bytes(work / "src" / "NOTICE", assets["NOTICE"])
        return assets

    def write_signature(self, build: str = "1", timestamp: bool = True) -> None:
        """The files the windows-sign agent and the timestamp step leave, made over the SHA256SUMS now in dist/."""
        dist = self.make_work(build) / "dist"
        p7s = fake_cms((dist / "SHA256SUMS").read_bytes())
        write_bytes(dist / "SHA256SUMS.p7s", p7s)
        if timestamp:
            write_bytes(dist / "SHA256SUMS.p7s.tsr", fake_tsr(p7s))

    def write_certificate(self, sha: str = SIGNER_SHA256, **changes) -> Path:
        """<tree>/jenkins/release-certificate.json, the pinned signer (D-058, D-059); a field changed to None is left out."""
        fields = {"sha256": sha, "subject": CERT_SUBJECT, "validUntil": "2099-01-01", **changes}
        body = ",".join(f'"{k}": "{v}"' for k, v in fields.items() if v is not None)
        path = self.root / "jenkins" / "release-certificate.json"
        write(path, "{" + body + "}\n")
        return path

    def write_record_input(self, build: str = "1", **pairs: str) -> Path:
        """work/record-input, the key=value lines the steps and the record command share."""
        path = self.make_work(build) / "record-input"
        write(path, "".join(f"{k}={v}\n" for k, v in pairs.items()))
        return path

    def native_calls(self) -> str:
        path = self.log_dir / "native.calls"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def run(self, command: str, args=(), env: dict | None = None, stdin: str = "",
            script: str = "release-steps.sh", timeout: int = 180, bash_flags=()) -> Result:
        """Runs jenkins/<script> <command> <args> in the tree and returns status, output and the stub calls.
        bash_flags are given to bash itself, for instance ("-x") to trace the whole run."""
        self.configure()
        full = clean_env()
        full.update(self.base_env)
        full.update(env or {})
        full["PATH"] = str(self.stubs) + os.pathsep + full.get("PATH", "")
        argv = [BASH, *bash_flags, (self.root / "jenkins" / script).as_posix(), command, *args]
        completed = subprocess.run(argv, cwd=self.root, env=full, input=stdin, capture_output=True, text=True,
                                   encoding="utf-8", timeout=timeout)
        return Result(completed, self.log_dir)

    def lib(self, function: str, *args: str, env: dict | None = None) -> Result:
        """Sources jenkins/release-lib.sh and calls one helper function."""
        return self.shell('. jenkins/release-lib.sh; "$@"', function, *args, env=env)

    def shell(self, snippet: str, *args: str, env: dict | None = None) -> Result:
        full = clean_env()
        full.update(env or {})
        completed = subprocess.run([BASH, "-c", snippet, "_", *args], cwd=self.root, env=full, input="",
                                   capture_output=True, text=True, encoding="utf-8", timeout=60)
        return Result(completed, self.log_dir)


class ReleaseCase(unittest.TestCase):
    """Base of the release test classes: self.new_tree() makes a throwaway tree removed at the end of the test."""

    def new_tree(self, **config) -> Tree:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tree = Tree(Path(holder.name), **config)
        tree.configure()
        return tree

    def setUp(self):
        self.tree = self.new_tree()


class FakeGitHub:
    """An in-memory GitHub repository behind a loopback HTTP server. Checks the Authorization header against a canary,
    records every request (method, path, query, headers, body) in order, and can inject failures."""

    def __init__(self, repository: str = REPOSITORY, token: str | None = None):
        self.repository = repository
        self.token = token or canary()
        self.requests: list = []
        self.refs: dict = {}
        self.tag_objects: dict = {}
        self.releases: dict = {}
        self.assets: dict = {}
        self.fail_upload_number: int | None = None
        self.uploads = 0
        self.altered: set = set()
        self.fail_download = False
        self.ignore_publish = False
        self.tsa_status = 200
        self.anonymous_reads = False
        self.hooks: dict = {}
        self.url_base: str | None = None
        self.next_id = 1
        self.server: ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeGitHub":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                fake.handle(self)

            do_POST = do_PATCH = do_DELETE = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def on(self, name: str, function) -> None:
        self.hooks[name] = function

    def identifier(self) -> int:
        self.next_id += 1
        return self.next_id

    def seed_head(self, branch: str, sha: str) -> None:
        self.refs[f"refs/heads/{branch}"] = sha

    def seed_tag(self, tag: str, sha: str, message: str | None = None) -> None:
        """A tag ref: annotated when a message is given, lightweight otherwise."""
        if message is None:
            self.refs[f"refs/tags/{tag}"] = sha
            return
        self.refs[f"refs/tags/{tag}"] = self.add_tag_object(tag, sha, message)

    def add_tag_object(self, tag: str, sha: str, message: str) -> str:
        key = hashlib.sha1(f"{tag}{sha}{message}{self.identifier()}".encode()).hexdigest()
        self.tag_objects[key] = {"sha": key, "tag": tag, "message": message, "object": sha}
        return key

    def seed_release(self, tag: str, draft: bool = True, prerelease: bool = False, files: dict | None = None) -> dict:
        release = {"id": self.identifier(), "tag_name": tag, "draft": draft, "prerelease": prerelease,
                   "body": "", "name": tag, "published_seq": 0 if draft else self.identifier()}
        self.releases[release["id"]] = release
        for name, data in (files or {}).items():
            self.add_asset(release["id"], name, data)
        return release

    def seed_published(self, tag: str, assets: dict, body: str = "") -> dict:
        """A published release with its assets, the state after a successful publish."""
        release = self.seed_release(tag, draft=False, files=assets)
        release["body"] = body
        return release

    def add_asset(self, release_id: int, name: str, data: bytes) -> dict:
        asset = {"id": self.identifier(), "release": release_id, "name": name, "data": data}
        self.assets[asset["id"]] = asset
        return asset

    def snapshot(self) -> dict:
        """What a later reader of the repository can see: refs, tag messages, releases and their assets."""
        return json.loads(json.dumps({
            "refs": self.refs,
            "messages": {sha: o["message"] for sha, o in self.tag_objects.items() if sha in self.refs.values()},
            "releases": {i: {k: v for k, v in r.items()} for i, r in self.releases.items()},
            "assets": sorted((a["release"], a["name"], hashlib.sha256(a["data"]).hexdigest())
                             for a in self.assets.values())}, sort_keys=True))

    def mutations(self, start: int = 0) -> list:
        return [r for r in self.requests[start:] if r["method"] != "GET"]

    def release_json(self, release: dict) -> dict:
        shown = {k: v for k, v in release.items() if k not in ("published_seq", "latest_seq")}
        shown["assets"] = [self.asset_json(a) for a in self.assets.values() if a["release"] == release["id"]]
        shown["upload_url"] = f"{self.url_base or self.base}/uploads/repos/{self.repository}/releases/{release['id']}/assets" \
                              "{?name,label}"
        return shown

    def asset_json(self, asset: dict) -> dict:
        release = self.releases.get(asset["release"], {})
        return {"id": asset["id"], "name": asset["name"], "size": len(asset["data"]),
                "url": f"{self.url_base or self.base}/repos/{self.repository}/releases/assets/{asset['id']}",
                "browser_download_url": f"{self.url_base or self.base}/download/{release.get('tag_name')}/{asset['name']}"}

    def served(self, asset: dict) -> bytes:
        data = asset["data"]
        return data + b"x" if asset["name"] in self.altered else data

    def latest(self) -> dict | None:
        published = [r for r in self.releases.values() if not r["draft"] and not r["prerelease"]
                     and r.get("make_latest") != "false"]
        return max(published, key=lambda r: r.get("latest_seq", r["published_seq"])) if published else None

    def handle(self, h: BaseHTTPRequestHandler) -> None:
        path, _, query = h.path.partition("?")
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        request = {"method": h.command, "path": path, "query": query, "headers": {k.lower(): v for k, v in
                                                                                  h.headers.items()}, "body": body}
        self.requests.append(request)
        status, payload = self.route(h.command, path, query, request, body)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/octet-stream" if isinstance(payload, bytes) else "application/json")
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        if h.command != "HEAD" and status != 204:
            h.wfile.write(raw)

    def route(self, method: str, path: str, query: str, request: dict, body: bytes):
        if path == "/tsa":
            return self.tsa(request, body)
        if path.startswith("/download/"):
            return self.download(path)
        public = f"/{self.repository}/releases/download/"
        if path.startswith(public):
            return self.download("/download/" + path[len(public):])
        prefix = f"/repos/{self.repository}"
        upload_prefix = f"/uploads{prefix}"
        if not (path.startswith(prefix) or path.startswith(upload_prefix)):
            return 404, {"message": "Not Found"}
        auth = request["headers"].get("authorization", "")
        if auth not in (f"Bearer {self.token}", f"token {self.token}") and not (self.anonymous_reads and method == "GET"):
            return 401, {"message": "Bad credentials"}
        rest = path[len(upload_prefix):] if path.startswith(upload_prefix) else path[len(prefix):]
        data = json.loads(body) if body and method in ("POST", "PATCH") and "/assets" not in rest else {}
        return self.dispatch(method, rest, query, request, data, body)

    def dispatch(self, method: str, rest: str, query: str, request: dict, data: dict, body: bytes):
        if rest.startswith("/git/"):
            return self.git_api(method, rest, data)
        if "/releases" in rest:
            return self.releases_api(method, rest, query, request, data, body)
        return 404, {"message": "Not Found"}

    def ref_json(self, ref: str) -> dict:
        sha = self.refs[ref]
        return {"ref": ref, "object": {"sha": sha, "type": "tag" if sha in self.tag_objects else "commit"}}

    def git_api(self, method: str, rest: str, data: dict):
        if method == "POST" and rest == "/git/tags":
            key = self.add_tag_object(data["tag"], data["object"], data["message"])
            return 201, dict(self.tag_objects[key], object={"sha": data["object"], "type": "commit"})
        if method == "POST" and rest == "/git/refs":
            if "create_ref" in self.hooks:
                self.hooks["create_ref"](data)
            if data["ref"] in self.refs:
                return 422, {"message": "Reference already exists"}
            self.refs[data["ref"]] = data["sha"]
            return 201, self.ref_json(data["ref"])
        match = re.match(r"/git/refs?/(.+)$", rest)
        if method == "GET" and match and "refs/" + match.group(1) in self.refs:
            return 200, self.ref_json("refs/" + match.group(1))
        if method == "DELETE" and match and "refs/" + match.group(1) in self.refs:
            del self.refs["refs/" + match.group(1)]
            return 204, {}
        match = re.match(r"/git/tags/([0-9a-f]+)$", rest)
        if method == "GET" and match and match.group(1) in self.tag_objects:
            tag = self.tag_objects[match.group(1)]
            return 200, dict(tag, object={"sha": tag["object"], "type": "commit"})
        return 404, {"message": "Not Found"}

    def releases_api(self, method: str, rest: str, query: str, request: dict, data: dict, body: bytes):
        if method == "POST" and rest == "/releases":
            return self.create_release(data)
        if method == "GET" and rest == "/releases/latest":
            latest = self.latest()
            return (200, self.release_json(latest)) if latest else (404, {"message": "Not Found"})
        match = re.match(r"/releases/tags/(.+)$", rest)
        if method == "GET" and match:
            found = [r for r in self.releases.values() if r["tag_name"] == match.group(1) and not r["draft"]]
            return (200, self.release_json(found[0])) if found else (404, {"message": "Not Found"})
        if method == "GET" and rest == "/releases":
            return 200, [self.release_json(r) for r in self.releases.values()]
        match = re.match(r"/releases/assets/(\d+)$", rest)
        if match:
            return self.asset_api(method, int(match.group(1)), request)
        match = re.match(r"/releases/(\d+)(/assets)?$", rest)
        if not match or int(match.group(1)) not in self.releases:
            return 404, {"message": "Not Found"}
        release = self.releases[int(match.group(1))]
        if match.group(2):
            return self.upload(method, release, query, body)
        return self.release_api(method, release, data)

    def create_release(self, data: dict):
        if any(r["tag_name"] == data["tag_name"] for r in self.releases.values()):
            return 422, {"message": "Validation Failed"}
        release = {"id": self.identifier(), "tag_name": data["tag_name"], "draft": bool(data.get("draft")),
                   "prerelease": bool(data.get("prerelease")), "body": data.get("body", ""),
                   "name": data.get("name", data["tag_name"]), "published_seq": 0}
        self.releases[release["id"]] = release
        return 201, self.release_json(release)

    def release_api(self, method: str, release: dict, data: dict):
        if method == "GET":
            return 200, self.release_json(release)
        if method == "DELETE":
            del self.releases[release["id"]]
            self.assets = {i: a for i, a in self.assets.items() if a["release"] != release["id"]}
            return 204, {}
        if "patch_release" in self.hooks:
            self.hooks["patch_release"](data)
        self.patch_release(release, data)
        return 200, self.release_json(release)

    def patch_release(self, release: dict, data: dict) -> None:
        if data.get("draft") is False and release["draft"] and self.ignore_publish:
            data = {k: v for k, v in data.items() if k != "draft"}
        for key in ("body", "name", "prerelease", "make_latest"):
            if key in data:
                release[key] = data[key]
        if data.get("draft") is False and release["draft"] and not self.ignore_publish:
            release["draft"], release["published_seq"] = False, self.identifier()
        if data.get("make_latest") == "true" and not release["draft"]:
            release["latest_seq"] = self.identifier()

    def upload(self, method: str, release: dict, query: str, body: bytes):
        if method != "POST":
            return 404, {"message": "Not Found"}
        self.uploads += 1
        if self.fail_upload_number == self.uploads:
            return 500, {"message": "upload failed (injected)"}
        name = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("name", "")
        return 201, self.asset_json(self.add_asset(release["id"], name, body))

    def asset_api(self, method: str, identifier: int, request: dict):
        asset = self.assets.get(identifier)
        if not asset:
            return 404, {"message": "Not Found"}
        if method == "DELETE":
            del self.assets[identifier]
            return 204, {}
        if "octet-stream" in request["headers"].get("accept", ""):
            return 200, self.served(asset)
        return 200, self.asset_json(asset)

    def tsa(self, request: dict, body: bytes):
        """The timestamp server: a POST of content type application/timestamp-query holding `tsq <hash>` is answered
        with `tsr <hash>` (the token the openssl stub verifies); tsa_status other than 200 makes it refuse."""
        if self.tsa_status != 200:
            return self.tsa_status, {"message": "refused"}
        if request["method"] != "POST" or request["headers"].get("content-type") != "application/timestamp-query" \
                or not body.startswith(b"tsq "):
            return 400, {"message": "not a timestamp query"}
        return 200, b"tsr " + body[4:]

    def download(self, path: str):
        if self.fail_download:
            return 503, {"message": "Service Unavailable"}
        _, _, tag, name = path.split("/", 3)
        for release in self.releases.values():
            if release["tag_name"] == tag and not release["draft"]:
                for asset in self.assets.values():
                    if asset["release"] == release["id"] and asset["name"] == name:
                        return 200, self.served(asset)
        return 404, {"message": "Not Found"}
