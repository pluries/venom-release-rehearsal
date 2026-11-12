#!/usr/bin/env python3
"""Tests of jenkins/release-image.sh (feature 011, tasks T060 to T067: the image commands of US4, against a stub
`docker` and the fake GitHub of scripts/site_release_fixture.py).

Contract these tests fix (all commands: `bash jenkins/release-image.sh <command>`, a refusal exits 1 with the reason on
standard error, success exits 0; every command that starts work first calls need_time, so a spent RELEASE_DEADLINE
exits 1 with "deadline" on standard error before any request or docker call):
- Environment: RELEASE_TAG, RELEASE_REPOSITORY (owner/name), RELEASE_IMAGE (registry/owner/name, no tag),
  RELEASE_REGISTRY_USER, RELEASE_API_BASE and RELEASE_DOWNLOAD_BASE (as release-github.sh), RELEASE_PUBLIC_URL,
  RELEASE_SHA (full; may be empty for an image-only run, then the peeled SHA of the public tag is used and a given one
  must equal it), JOB_NAME, BUILD_NUMBER, RELEASE_WORK_ROOT, optional RELEASE_DEADLINE.
- Work files under <work>/image/: release/ (the downloaded files), src/ (the public clone at the SHA), fetched.ok,
  context/ (build context), files.txt (file list), verified.ok.
- Anonymous commands, nothing on standard input: fetch, build, smoke, files. Registry commands read the token as one
  line on standard input and log in with `docker login -u $RELEASE_REGISTRY_USER --password-stdin <registry host>` into a
  temporary DOCKER_CONFIG that is removed on exit: check-tag, push, verify, latest.
- fetch (D-059: Certum signs SHA256SUMS, minisign and check-key are gone): downloads SHA256SUMS, SHA256SUMS.p7s and
  venom.linux-amd64 anonymously from <download base>/<repository>/releases/download/<tag>/<name> (a draft or absent
  release answers 404: exit 1, "published" on standard error); clones RELEASE_PUBLIC_URL at the SHA; runs
  `openssl cms -verify -inform DER -in release/SHA256SUMS.p7s -content release/SHA256SUMS -binary -purpose any -signer
  <tmp>/signer.pem -out /dev/null`, then compares the SHA-256 of that signer with the sha256 of
  jenkins/release-certificate.json beside the script (a failure names `signature` or `signer`); checks the hash of
  venom.linux-amd64 against the signed list; when <work>/dist/SHA256SUMS exists, also against its
  entry (exit 1, names the file and SHA256SUMS); only then writes fetched.ok (tag=, sha=). build, push, verify, latest,
  smoke refuse (exit 1) without a fetched.ok for RELEASE_TAG.
- check-tag: login, then `docker manifest inspect <image>:<tag>`. Status 0 -> exit 1 "already exists". Status non-zero with
  standard error starting `no such manifest` -> exit 0 "is free". Any other answer -> exit 1 "cannot read the registry".
- build: context/ holds exactly Dockerfile, LICENSE and NOTICE (from the clone) and dist/venom.linux-amd64 (the
  downloaded file); one `docker build -f <context>/Dockerfile -t <image>:sha-<first 7 of SHA> <context>`: the version tag
  does not exist, not even locally, before verify (owner decision T078).
- push: login, `docker push <image>:sha-<7>` and nothing else (no push of `<image>:<tag>`); reads `sha-<7>: digest:
  sha256:<64 hex>` from the output and records `image_digest=<image>@sha256:<hex>` in record-input; a failed push exits
  1, records nothing. The sha-<7> tag may be pushed again by a retry (overwritten); check-tag looks at <tag> only.
- verify: login (once), `docker pull <image>@<digest>`, `docker create`, `docker cp <id>:/usr/local/bin/venom <file>`,
  `docker rm`; the file's hash must equal the signed entry (exit 1 otherwise, names venom.linux-amd64, and then no
  `docker tag` or `docker push` of `<image>:<tag>` happens); on a pass it writes verified.ok, then runs `docker tag
  <image>@<digest> <image>:<tag>` and `docker push <image>:<tag>` (owner decision T078).
- latest: refuses without verified.ok; reads GET <API base>/repos/<repository>/releases anonymously; the highest tag, by
  version (base, then n, numbers as numbers), among the published (not draft), not withdrawn (not prerelease, body not
  starting WITHDRAWN) releases other than RELEASE_TAG; with none, or when RELEASE_TAG is higher: login,
  `docker tag <image>@<verified digest> <image>:latest`, `docker push <image>:latest`; otherwise exit 0 and nothing moves.
- smoke: `docker create <image>:<tag>` (prints the container id), `docker cp <src>/site/examples/quickstart.yml
  <id>:/workdir/tests/`, `docker start -a <id>`, `docker rm`; exit 0 only when start exits 0 and prints
  `final status: PASS`, then prints `smoke: PASS`. No `docker run`, no volume or mount option.
- files: `docker create <image>:<tag>`, `docker export <id>` listed with `tar -t` into files.txt, then
  `release-leak-check.sh files <files.txt>`; its status is the command's.
- `release-steps.sh record` with RELEASE_RUN_MODE=image-only writes release-record-image-<yyyymmddThhmmssZ>.md in the work
  directory and leaves release-record.md as it is.
Skipped, with the reason printed, when a tool or the private paths are missing. Offline, standard library only.
"""
import hashlib
import io
import re
import tarfile
import unittest
from pathlib import Path

from site_release_fixture import (PRELUDE, SIGNER_SHA256, TAG, FakeGitHub, ReleaseCase, canary, git, parse_calls,
                                  requires_release, write, write_bytes)

IMAGE = "registry.example.invalid/pluries-example/venom-example"
USER = "registry-user"
DIGEST = "sha256:" + "ab" * 32
LOGIN_COMMANDS = ("check-tag", "push", "verify", "latest")
ORDER = ("fetch", "build", "push", "verify")
EXAMPLE = "name: quickstart\ntestcases:\n  - name: hello\n    steps:\n      - type: exec\n        script: echo hello\n"
GOOD_FILES = ("usr/local/bin/venom", "LICENSE", "NOTICE", "etc/os-release")
CONTAINER = "stubcontainer"

IMAGE_DOCKER_STUB = PRELUDE + """\
if [ "${STUB_DOCKER_FAIL:-}" = "${1:-}" ]; then echo "docker $1 refused" >&2; exit 1; fi
case "${1:-}" in
  login)
    cat > "$stub_dir/log/docker.login.stdin"
    printf '%s\\n' "${DOCKER_CONFIG:-}" >> "$stub_dir/log/docker.config.paths"
    if command -v cygpath >/dev/null 2>&1; then cygpath -m "${DOCKER_CONFIG:-}"; else echo "${DOCKER_CONFIG:-}"; fi \\
      >> "$stub_dir/log/docker.config.native"
    if [ -d "${DOCKER_CONFIG:-}" ]; then echo auth > "$DOCKER_CONFIG/config.json"; fi ;;
  manifest)
    if [ "${STUB_DOCKER_MANIFEST_RC:-1}" = 0 ]; then echo '{"schemaVersion": 2}'; exit 0; fi
    printf '%s\\n' "${STUB_DOCKER_MANIFEST_OUT-no such manifest}" >&2; exit "${STUB_DOCKER_MANIFEST_RC:-1}" ;;
  push) printf '%s: digest: %s size: 527\\n' "${2##*:}" "$STUB_DOCKER_DIGEST" ;;
  create) echo stubcontainer ;;
  cp)
    case "${2:-}" in
      *:/usr/local/bin/venom) cp "$STUB_DOCKER_IMAGE_BINARY" "$3" ;;
      *) mkdir -p "$stub_dir/log/in-container"; cp "$2" "$stub_dir/log/in-container/" ;;
    esac ;;
  start) printf '%s\\n' "${STUB_DOCKER_START_OUT-final status: PASS}"; exit "${STUB_DOCKER_START_RC:-0}" ;;
  export) cat "$STUB_DOCKER_EXPORT_TAR" ;;
esac
"""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signed_entry(sums: bytes, name: str) -> str:
    return dict(line.split("  ", 1)[::-1] for line in sums.decode().splitlines())[name]


def make_tar(path: Path, names) -> None:
    with tarfile.open(path, "w") as tar:
        for name in names:
            data = b"content of " + name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def holding(roots: list, value: str, skip: str) -> list:
    needle = value.encode()
    return [p for root in roots for p in Path(root).rglob("*")
            if p.is_file() and p.name != skip and needle in p.read_bytes()]


class ImageFlow:
    """A published release (or a draft, or none) on a FakeGitHub that answers GET anonymously, its files in the work
    directory of build 1 when build_files is set (the release job's own run), a public repository holding the tag and
    the quickstart example, the pinned signer certificate in the tree's jenkins/, and a stub docker whose behaviour comes from the tree's settings. Without build_files the
    run is an image-only one: build 2, an empty work directory and an empty RELEASE_SHA."""

    def __init__(self, case: ReleaseCase, tag: str = TAG, build_files: bool = True, release: str = "published",
                 others=(), after=(), **config):
        self.case, self.tag = case, tag
        self.build = "1" if build_files else "2"
        self.tree = case.new_tree(**config)
        write(self.tree.stubs / "docker", IMAGE_DOCKER_STUB, executable=True)
        self.fake = FakeGitHub().start()
        self.fake.anonymous_reads = True
        case.addCleanup(self.fake.stop)
        self.sha = self.seed_public()
        self.assets = self.tree.prepare_release_files()
        self.tree.write_certificate()
        self.seed_releases(release, others, after)
        self.token = canary("registry", "token", "not", "real", "4712")
        self.arrange_docker()
        self.env = {"RELEASE_API_BASE": self.fake.base, "RELEASE_DOWNLOAD_BASE": self.fake.base,
                    "RELEASE_REPOSITORY": self.fake.repository, "RELEASE_TAG": tag, "RELEASE_SHA": self.sha,
                    "RELEASE_IMAGE": IMAGE, "RELEASE_REGISTRY_USER": USER, "BUILD_NUMBER": self.build}
        if not build_files:
            self.env["RELEASE_SHA"] = ""

    def seed_public(self) -> str:
        tree, seed = self.tree, self.tree.seed
        write(seed / "site" / "examples" / "quickstart.yml", EXAMPLE)
        git(tree.git_env, "add", ".", cwd=seed)
        git(tree.git_env, "commit", "-q", "-m", "add the example", cwd=seed)
        sha = git(tree.git_env, "rev-parse", "HEAD", cwd=seed)
        git(tree.git_env, "push", "-q", str(tree.public), "master", cwd=seed)
        tree.public_tag(self.tag, annotated=True)
        return sha

    def seed_releases(self, release: str, others, after) -> None:
        for other in others:
            self.seed_other(*other)
        if release == "published":
            self.fake.seed_published(self.tag, self.assets)
        elif release == "draft":
            self.fake.seed_release(self.tag, draft=True, files=self.assets)
        for other in after:
            self.seed_other(*other)

    def seed_other(self, tag: str, kind: str = "published") -> None:
        if kind == "draft":
            self.fake.seed_release(tag, draft=True)
            return
        release = self.fake.seed_published(tag, {})
        if kind == "withdrawn":
            release["prerelease"], release["body"] = True, "WITHDRAWN: a reason\n\nnotes"

    def arrange_docker(self, binary: bytes | None = None, files=GOOD_FILES) -> None:
        image = self.tree.tmp / "image-binary"
        write_bytes(image, self.assets["venom.linux-amd64"] if binary is None else binary)
        make_tar(self.tree.tmp / "export.tar", files)
        self.tree.settings.update({"STUB_DOCKER_IMAGE_BINARY": image.as_posix(), "STUB_DOCKER_DIGEST": DIGEST,
                                   "STUB_DOCKER_EXPORT_TAR": (self.tree.tmp / "export.tar").as_posix()})

    def run(self, command: str, *args: str, flags=(), script: str = "release-image.sh", **extra):
        stdin = self.token + "\n" if command in LOGIN_COMMANDS and script == "release-image.sh" else ""
        return self.tree.run(command, list(args), env={**self.env, **extra}, stdin=stdin, script=script,
                             bash_flags=flags)

    def step(self, command: str, *args: str, **kwargs):
        result = self.run(command, *args, **kwargs)
        self.case.assertEqual(result.returncode, 0, f"{command}: {result.stderr[-600:]}")
        return result

    def through(self, last: str) -> None:
        for command in ORDER:
            self.step(command)
            if command == last:
                return

    def work(self) -> Path:
        return self.tree.work_dir(self.build)

    def calls(self, stub: str) -> list:
        path = self.tree.log_dir / f"{stub}.calls"
        return parse_calls(path.read_text(encoding="utf-8")) if path.is_file() else []

    def verbs(self) -> list:
        return [c.argv[0] for c in self.calls("docker") if c.argv]

    def docker(self, verb: str) -> list:
        return [c for c in self.calls("docker") if c.argv[:1] == [verb]]

    def moved_latest(self) -> bool:
        return any(c.argv == ["push", f"{IMAGE}:latest"] for c in self.calls("docker"))

    def registry_untouched(self) -> bool:
        return not set(self.verbs()) & {"build", "push", "tag"}


@requires_release
class ReleaseImage(ReleaseCase):
    """Tasks T060 to T067: the image commands (US4-AS1 to AS10, FR-008, FR-012, NFR-001, NFR-005, NFR-008, NFR-013)."""

    def setUp(self):
        self.flow_cache = None

    @property
    def flow(self) -> ImageFlow:
        if self.flow_cache is None:
            self.flow_cache = ImageFlow(self)
        return self.flow_cache

    # ---- T060: the context and the binary ----

    def test_the_image_is_built_from_the_dockerfile_of_the_public_clone_with_a_context_of_four_files(self):
        """Task T060, T079, US4-AS1, FR-008, NFR-008: the context holds the clone's Dockerfile, LICENSE and NOTICE and
        the downloaded binary."""
        flow = self.flow
        flow.through("build")
        context = flow.work() / "image" / "context"
        files = sorted(p.relative_to(context).as_posix() for p in context.rglob("*") if p.is_file())
        self.assertEqual(files, ["Dockerfile", "LICENSE", "NOTICE", "dist/venom.linux-amd64"])
        self.assertEqual((context / "Dockerfile").read_text(encoding="utf-8"), "FROM scratch\n")
        self.assertEqual((context / "LICENSE").read_bytes(), flow.assets["LICENSE"])
        self.assertEqual((context / "NOTICE").read_bytes(), flow.assets["NOTICE"])
        builds = flow.docker("build")
        self.assertEqual(len(builds), 1)
        argv = builds[0].argv
        self.assertEqual(Path(argv[argv.index("-f") + 1]), context / "Dockerfile")
        self.assertEqual(Path(argv[-1]), context)

    def test_the_binary_in_the_context_is_the_downloaded_release_asset_whose_hash_equals_the_signed_list(self):
        """Task T060, US4-AS1, FR-008: the binary is the published asset, its hash is the signed entry; a download that
        differs from the signed list stops fetch and nothing is built."""
        flow = self.flow
        flow.through("build")
        binary = (flow.work() / "image" / "context" / "dist" / "venom.linux-amd64").read_bytes()
        self.assertEqual(binary, flow.assets["venom.linux-amd64"])
        self.assertEqual(sha256_of(binary), signed_entry(flow.assets["SHA256SUMS"], "venom.linux-amd64"))
        downloads = [r for r in flow.fake.requests if r["path"].endswith("/venom.linux-amd64")]
        self.assertTrue(downloads)
        tampered = ImageFlow(self)
        tampered.fake.altered.add("venom.linux-amd64")
        refused = tampered.run("fetch")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("venom.linux-amd64", refused.stderr)
        self.assertEqual(tampered.run("build").returncode, 1)
        self.assertNotIn("build", tampered.verbs())

    def test_the_downloaded_file_equals_the_entry_of_the_builds_sha256sums(self):
        """Task T060, FR-008, open point 16: the release job's own dist/SHA256SUMS names the same hash; a different
        entry stops fetch, names the file and writes no fetched.ok."""
        flow = self.flow
        flow.step("fetch")
        build_sums = (flow.work() / "dist" / "SHA256SUMS").read_bytes()
        downloaded = (flow.work() / "image" / "release" / "venom.linux-amd64").read_bytes()
        self.assertEqual(sha256_of(downloaded), signed_entry(build_sums, "venom.linux-amd64"))
        other = ImageFlow(self)
        sums = other.work() / "dist" / "SHA256SUMS"
        entry = signed_entry(sums.read_bytes(), "venom.linux-amd64")
        sums.write_text(sums.read_text().replace(entry, sha256_of(b"another build")), encoding="utf-8", newline="\n")
        refused = other.run("fetch")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("venom.linux-amd64", refused.stderr)
        self.assertIn("SHA256SUMS", refused.stderr)
        self.assertFalse((other.work() / "image" / "fetched.ok").exists())

    # ---- T061: tags and latest ----

    def test_the_image_gets_the_release_tag_and_sha_dash_first_seven(self):
        """Task T061, T078, US4-AS2: built under sha-<7> only and pushed under it; the release tag is tagged from the
        digest and pushed by verify, after it passed; no latest from push or verify; digest recorded (owner decision T078:
        the build and push no longer name the release tag)."""
        flow = self.flow
        flow.through("push")
        sha_ref = f"{IMAGE}:sha-{flow.sha[:7]}"
        argv = flow.docker("build")[0].argv
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-t"], [sha_ref])
        self.assertEqual([c.argv[1] for c in flow.docker("push")], [sha_ref])
        record = (flow.work() / "record-input").read_text(encoding="utf-8")
        self.assertRegex(record, rf"(?m)^image_digest={re.escape(IMAGE)}@{DIGEST}$")
        flow.step("verify")
        pushed = [c.argv[1] for c in flow.docker("push")]
        self.assertEqual(pushed.count(f"{IMAGE}:{TAG}"), 1, pushed)
        self.assertIn(sha_ref, pushed)
        self.assertIn(["tag", f"{IMAGE}@{DIGEST}", f"{IMAGE}:{TAG}"], [c.argv for c in flow.docker("tag")])
        self.assertFalse(flow.moved_latest())

    def test_the_tag_just_published_is_already_the_github_latest_and_the_image_latest_still_moves(self):
        """Task T061, FR-012, M4: after publish the GitHub latest is the tag itself; the comparison is with the others."""
        flow = ImageFlow(self, others=[("v1.4.0-pluries.1", "published")])
        self.assertEqual(flow.fake.latest()["tag_name"], TAG)
        flow.through("verify")
        flow.step("latest")
        self.assertTrue(flow.moved_latest())
        self.assertIn(["tag", f"{IMAGE}@{DIGEST}", f"{IMAGE}:latest"], [c.argv for c in flow.docker("tag")])

    def test_fetch_refuses_a_public_url_that_does_not_end_with_the_repository_naming_both_variables(self):
        """Review finding 8: RELEASE_PUBLIC_URL and RELEASE_REPOSITORY must name the same repository."""
        flow = self.flow
        result = flow.run("fetch", RELEASE_REPOSITORY="pluries-example/another-repository")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RELEASE_PUBLIC_URL", result.stderr)
        self.assertIn("RELEASE_REPOSITORY", result.stderr)
        self.assertFalse((flow.work() / "image" / "fetched.ok").exists())

    def test_latest_and_the_sha_tag_are_tagged_from_the_verified_digest_never_from_the_local_name(self):
        """Review finding 3: every `docker tag` after push takes the recorded digest as its source, so a local name that
        was rebuilt or replaced cannot reach the registry under latest or sha-<7>."""
        flow = self.flow
        flow.through("verify")
        flow.step("latest")
        tags = {c.argv[2]: c.argv[1] for c in flow.docker("tag")}
        self.assertEqual(tags.get(f"{IMAGE}:latest"), f"{IMAGE}@{DIGEST}")
        self.assertEqual(tags.get(f"{IMAGE}:sha-{flow.sha[:7]}"), f"{IMAGE}@{DIGEST}")
        pushes = [c.argv[1] for c in flow.docker("push")]
        self.assertIn(f"{IMAGE}:sha-{flow.sha[:7]}", pushes)

    def test_latest_is_compared_with_the_highest_other_published_non_withdrawn_tag_also_in_image_only_mode(self):
        """Task T061, FR-012, open point 28: a withdrawn or draft higher tag does not count; a published one does. The
        image-only run (build 2, no files of its own) takes the same decision from the public releases."""
        ignored = [("v1.4.0-pluries.1", "published"), ("v1.9.0-pluries.1", "withdrawn"), ("v1.8.0-pluries.1", "draft")]
        for others, moved in ((ignored, True), (ignored + [("v1.5.0-pluries.1", "published")], False)):
            with self.subTest(moved=moved):
                flow = ImageFlow(self, build_files=False, others=others)
                flow.through("verify")
                flow.step("latest")
                self.assertEqual(flow.moved_latest(), moved)

    def test_latest_moves_only_for_a_release_greater_than_the_current_latest(self):
        """Task T061, FR-012: 1.4.0-pluries.2 over 1.4.0-pluries.1, 1.5.0-pluries.1 over 1.4.0-pluries.9 (numbers as
        numbers), a lower one, or pluries.2 under pluries.10, does not move it."""
        cases = [(TAG, "v1.4.0-pluries.1", True), ("v1.5.0-pluries.1", "v1.4.0-pluries.9", True),
                 ("v1.4.0-pluries.1", "v1.4.0-pluries.2", False), (TAG, "v1.4.0-pluries.10", False)]
        for tag, other, moved in cases:
            with self.subTest(tag=tag, other=other):
                flow = ImageFlow(self, tag=tag, others=[(other, "published")])
                flow.through("verify")
                result = flow.step("latest")
                self.assertEqual(flow.moved_latest(), moved, result.stdout)

    def test_latest_is_not_moved_before_verify_passes(self):
        """Task T061, FR-012: after push and before verify, latest refuses and moves nothing."""
        flow = self.flow
        flow.through("push")
        refused = flow.run("latest")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertFalse(flow.moved_latest())
        self.assertNotIn("tag", flow.verbs())

    def test_with_no_other_published_tag_the_first_release_moves_latest(self):
        """Task T061, FR-012: the only published release is this one; latest moves."""
        flow = self.flow
        flow.through("verify")
        flow.step("latest")
        self.assertTrue(flow.moved_latest())

    def test_when_the_github_latest_flag_and_the_highest_tag_differ_the_highest_version_wins_and_the_flag_is_ignored(self):
        """Task T061, open point 28: GitHub's latest flag sits on v1.3.0-pluries.9 (published last) but v1.5.0-pluries.1
        is higher than this tag; latest stays where it is."""
        flow = ImageFlow(self, after=[("v1.5.0-pluries.1", "published"), ("v1.3.0-pluries.9", "published")])
        self.assertEqual(flow.fake.latest()["tag_name"], "v1.3.0-pluries.9")
        flow.through("verify")
        flow.step("latest")
        self.assertFalse(flow.moved_latest())

    # ---- T062: unpublished releases ----

    def refusals(self, flow: ImageFlow) -> None:
        self.assertEqual(flow.run("fetch").returncode, 1)
        for command in ("build", "push", "verify", "latest", "smoke"):
            self.assertEqual(flow.run(command).returncode, 1, command)

    def test_the_image_commands_refuse_a_tag_whose_release_is_a_draft_or_absent(self):
        """Task T062, US4-AS6: fetch names the missing publication; every later command refuses too."""
        for state in ("draft", "absent"):
            with self.subTest(state=state):
                flow = ImageFlow(self, release=state)
                refused = flow.run("fetch")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("published", refused.stderr)
                self.refusals(flow)

    def test_no_tag_and_no_latest_exist_after_a_refusal(self):
        """Task T062, US4-AS6: no build, push or tag call, and nothing written to GitHub, after a refusal."""
        for state in ("draft", "absent"):
            with self.subTest(state=state):
                flow = ImageFlow(self, release=state)
                self.refusals(flow)
                self.assertTrue(flow.registry_untouched(), flow.verbs())
                self.assertFalse(any(a.endswith(":latest") for c in flow.calls("docker") for a in c.argv))
                self.assertEqual(flow.fake.mutations(), [])

    # ---- T063: verify and check-tag ----

    def test_verify_extracts_the_binary_from_the_pushed_image_and_its_hash_equals_the_signed_list(self):
        """Task T063, US4-AS9, FR-012: login, pull by the recorded digest, create, cp of /usr/local/bin/venom."""
        flow = self.flow
        flow.through("push")
        flow.step("verify")
        verbs = flow.verbs()
        self.assertLess(len(verbs) - 1 - verbs[::-1].index("login"), verbs.index("pull"))
        self.assertEqual(flow.docker("pull")[0].argv, ["pull", f"{IMAGE}@{DIGEST}"])
        extracts = [c.argv[1] for c in flow.docker("cp")]
        self.assertTrue(any(a.endswith(":/usr/local/bin/venom") for a in extracts), extracts)
        self.assertTrue((flow.work() / "image" / "verified.ok").is_file())

    def test_a_different_hash_fails(self):
        """Task T063, US4-AS9: an extracted binary that is not the signed one fails verify and keeps latest back."""
        flow = ImageFlow(self)
        flow.arrange_docker(binary=b"not the released binary\n")
        flow.through("push")
        refused = flow.run("verify")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("venom.linux-amd64", refused.stderr)
        self.assertFalse((flow.work() / "image" / "verified.ok").exists())
        self.assertEqual(flow.run("latest").returncode, 1)
        self.assertFalse(flow.moved_latest())

    def test_a_failed_verify_leaves_no_version_tag_and_no_latest(self):
        """Task T078, FR-012, US4-AS9 (owner decision): the binary in the image is not the signed one; verify fails and
        no `docker tag` or `docker push` of <image>:<tag> or <image>:latest was ever run: only sha-<7> is in the registry."""
        flow = ImageFlow(self)
        flow.arrange_docker(binary=b"not the released binary\n")
        flow.through("push")
        refused = flow.run("verify")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("venom.linux-amd64", refused.stderr)
        for call in flow.calls("docker"):
            if call.argv[0] in ("tag", "push"):
                self.assertNotIn(f"{IMAGE}:{TAG}", call.argv, call.argv)
                self.assertNotIn(f"{IMAGE}:latest", call.argv, call.argv)
        self.assertEqual([c.argv[1] for c in flow.docker("push")], [f"{IMAGE}:sha-{flow.sha[:7]}"])
        self.assertEqual(flow.run("latest").returncode, 1)
        self.assertFalse(flow.moved_latest())

    def test_the_version_tag_is_pushed_only_after_verify_passes(self):
        """Task T078, FR-012 (owner decision): after push the registry holds sha-<7> alone; verify pulls by digest and
        extracts the binary first, then tags the digest as <tag> and pushes it, under the one login of verify."""
        flow = self.flow
        flow.through("push")
        version = f"{IMAGE}:{TAG}"
        self.assertEqual([c.argv[1] for c in flow.docker("push")], [f"{IMAGE}:sha-{flow.sha[:7]}"])
        self.assertNotIn("tag", flow.verbs())
        before = len(flow.calls("docker"))
        flow.step("verify")
        calls = [c.argv for c in flow.calls("docker")][before:]
        verbs = [a[0] for a in calls]
        self.assertEqual(verbs.count("login"), 1, verbs)
        tag_at = calls.index(["tag", f"{IMAGE}@{DIGEST}", version])
        push_at = calls.index(["push", version])
        cp_at = max(i for i, a in enumerate(calls) if a[0] == "cp")
        self.assertLess(verbs.index("pull"), cp_at)
        self.assertLess(cp_at, tag_at)
        self.assertLess(tag_at, push_at)
        self.assertTrue((flow.work() / "image" / "verified.ok").is_file())

    def test_a_retry_after_a_failed_verify_can_reuse_the_tag(self):
        """Task T078, FR-012 (owner decision): a failed verify burns nothing: check-tag still finds <tag> free, the whole
        image sequence runs again with a good binary and pushes <tag> exactly once; sha-<7> is pushed again (overwritten:
        check-tag looks at <tag> only)."""
        flow = ImageFlow(self)
        flow.arrange_docker(binary=b"not the released binary\n")
        flow.through("push")
        self.assertEqual(flow.run("verify").returncode, 1)
        flow.arrange_docker()
        retry = flow.step("check-tag")
        self.assertIn("is free", retry.stdout)
        for command in ORDER:
            flow.step(command)
        flow.step("latest")
        pushed = [c.argv[1] for c in flow.docker("push")]
        self.assertEqual(pushed.count(f"{IMAGE}:{TAG}"), 1, pushed)
        self.assertGreaterEqual(pushed.count(f"{IMAGE}:sha-{flow.sha[:7]}"), 2, pushed)
        self.assertTrue(flow.moved_latest())

    def test_check_tag_runs_after_login_and_stops_when_the_image_tag_exists_and_nothing_is_overwritten(self):
        """Task T063, US4-AS10: login first, then manifest inspect; status 0 means the tag exists and the run stops."""
        flow = ImageFlow(self, STUB_DOCKER_MANIFEST_RC="0")
        refused = flow.run("check-tag")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("already exists", refused.stderr)
        self.assertEqual(flow.verbs(), ["login", "manifest"])
        self.assertEqual(flow.docker("manifest")[0].argv, ["manifest", "inspect", f"{IMAGE}:{TAG}"])

    def test_check_tag_goes_on_when_the_registry_says_no_such_manifest(self):
        """Task T063, US4-AS10: the answer `no such manifest` (alone, or followed by the reference) is read as absent."""
        for answer in ("no such manifest", f"no such manifest: {IMAGE}:{TAG}"):
            with self.subTest(answer=answer):
                flow = ImageFlow(self, STUB_DOCKER_MANIFEST_OUT=answer)
                result = flow.step("check-tag")
                self.assertIn("is free", result.stdout)
                self.assertEqual(flow.verbs(), ["login", "manifest"])

    def test_check_tag_stops_as_an_error_on_an_authorisation_or_network_answer_and_never_reads_it_as_absent(self):
        """Task T063, US4-AS10: any other answer, even one that holds the words in the middle, is an error."""
        answers = ["unauthorized: authentication required", "denied: requested access to the resource is denied",
                   'Get "https://registry.example.invalid/v2/": dial tcp: lookup registry.example.invalid: no such host',
                   "denied: no such manifest in this scope", ""]
        for answer in answers:
            with self.subTest(answer=answer):
                flow = ImageFlow(self, STUB_DOCKER_MANIFEST_OUT=answer)
                refused = flow.run("check-tag")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("cannot read the registry", refused.stderr)
                self.assertNotIn("is free", refused.stdout)

    # ---- T064: the file list ----

    def test_the_file_list_of_the_image_must_hold_the_licence_and_the_notice(self):
        """Task T064, T079, US4-AS4, NFR-008: a list with LICENSE and NOTICE passes; one without either stops the run and
        names the missing file."""
        flow = self.flow
        flow.through("build")
        flow.step("files")
        listed = (flow.work() / "image" / "files.txt").read_text(encoding="utf-8")
        self.assertIn("LICENSE", listed.splitlines())
        self.assertIn("NOTICE", listed.splitlines())
        self.assertEqual(flow.docker("create")[0].argv, ["create", f"{IMAGE}:sha-{flow.sha[:7]}"])
        self.assertTrue(flow.docker("export"))
        bare = ImageFlow(self)
        bare.arrange_docker(files=("usr/local/bin/venom", "etc/os-release"))
        bare.through("build")
        refused = bare.run("files")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("LICENSE", refused.stderr)
        self.assertIn("NOTICE", refused.stderr)
        for kept, missing in (("LICENSE", "NOTICE"), ("NOTICE", "LICENSE")):
            with self.subTest(missing=missing):
                half = ImageFlow(self)
                half.arrange_docker(files=("usr/local/bin/venom", kept, "etc/os-release"))
                half.through("build")
                refused = half.run("files")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(f"no entry named {missing}", refused.stderr)

    def test_a_planted_private_path_or_host_in_the_file_list_stops_the_run(self):
        """Task T064, US4-AS4, NFR-005: a private path or a private network address among the entries is a leak."""
        for planted in ("app/Users/builder/notes.txt", "tmp/10.20.30.40.log"):
            with self.subTest(planted=planted):
                flow = ImageFlow(self)
                flow.arrange_docker(files=(*GOOD_FILES, planted))
                flow.through("build")
                refused = flow.run("files")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("leak:", refused.stderr)

    # ---- T065: the registry token ----

    def test_the_registry_token_goes_on_password_stdin_into_a_temporary_docker_config_removed_on_exit_and_is_in_no_argv_no_log_even_under_bash_x(self):
        """Task T065, US4-AS5, NFR-001, NFR-013: every registry command logs in once with the token on standard input
        into its own temporary DOCKER_CONFIG, removed afterwards; the token is in no argv, environment, output or file."""
        flow = self.flow
        token = flow.token
        results = [flow.step("fetch"), flow.step("build")]
        stdin_file = flow.tree.log_dir / "docker.login.stdin"
        for command in LOGIN_COMMANDS:
            results.append(flow.step(command, flags=("-x",)))
            self.assertEqual(stdin_file.read_text(encoding="utf-8").strip(), token, command)
        logins = flow.docker("login")
        self.assertEqual(len(logins), len(LOGIN_COMMANDS))
        for call in logins:
            self.assertEqual(call.argv[:3], ["login", "-u", USER])
            self.assertIn("--password-stdin", call.argv)
        paths = (flow.tree.log_dir / "docker.config.paths").read_text(encoding="utf-8").split()
        native = (flow.tree.log_dir / "docker.config.native").read_text(encoding="utf-8").split()
        self.assertEqual(len(set(paths)), len(LOGIN_COMMANDS), paths)
        for path in native:
            self.assertFalse(Path(path).exists(), f"{path} was not removed")
            self.assertFalse(Path(path).is_relative_to(flow.tree.tmp), f"{path} is inside the tree")
        used = {c.env.get("DOCKER_CONFIG") for v in ("manifest", "push", "pull") for c in flow.docker(v)}
        self.assertTrue(used and used <= set(paths), used)
        self.assert_no_token(flow, results, stdin_file.name)

    def assert_no_token(self, flow: ImageFlow, results: list, allowed: str) -> None:
        token = flow.token
        for result in results:
            self.assertNotIn(token, result.output())
        for stub in ("docker", "curl", "git", "openssl"):
            for call in flow.calls(stub):
                self.assertNotIn(token, "\n".join(call.argv + list(call.env.values())), stub)
        self.assertEqual(holding([flow.tree.work_root, flow.tree.root], token, allowed), [])
        self.assertEqual(holding([flow.tree.log_dir], token, allowed), [])

    # ---- T066: push failure and the image-only run ----

    def test_a_push_that_fails_leaves_the_release_exits_non_zero_and_the_record_says_image_not_published(self):
        """Task T066, US4-AS8: the release is as it was, no digest is recorded, the record says so, latest stays."""
        flow = ImageFlow(self, STUB_DOCKER_FAIL="push")
        flow.through("build")
        before = flow.fake.snapshot()
        failed = flow.run("push")
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertEqual(flow.fake.snapshot(), before)
        self.assertEqual(flow.fake.mutations(), [])
        record_input = flow.work() / "record-input"
        self.assertNotIn("image_digest", record_input.read_text(encoding="utf-8") if record_input.exists() else "")
        flow.step("record", script="release-steps.sh", RELEASE_STATUS="failed")
        self.assertIn("image not published", (flow.work() / "release-record.md").read_text(encoding="utf-8"))

    def test_latest_has_not_moved_after_a_failed_push(self):
        """Task T066, US4-AS8: after a failed push, verify and latest refuse and no latest tag is pushed."""
        flow = ImageFlow(self, STUB_DOCKER_FAIL="push")
        flow.through("build")
        self.assertEqual(flow.run("push").returncode, 1)
        for command in ("verify", "latest"):
            self.assertEqual(flow.run(command).returncode, 1, command)
        self.assertFalse(flow.moved_latest())

    def test_an_image_only_run_writes_a_new_record_file_and_never_overwrites_the_release_record(self):
        """Task T066, US4-AS8, FR-009: with RELEASE_RUN_MODE=image-only, record writes release-record-image-<UTC>.md next
        to the untouched release-record.md and names the tag and the digest (cause today: assertion, existing member)."""
        flow = ImageFlow(self)
        flow.tree.write_record_input(flow.build, image_digest=f"{IMAGE}@{DIGEST}")
        flow.step("record", script="release-steps.sh", RELEASE_STATUS="success")
        original = (flow.work() / "release-record.md").read_bytes()
        flow.step("record", script="release-steps.sh", RELEASE_STATUS="success", RELEASE_RUN_MODE="image-only")
        self.assertEqual((flow.work() / "release-record.md").read_bytes(), original)
        found = sorted(flow.work().glob("release-record-image-*.md"))
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0].name, r"^release-record-image-\d{8}T\d{6}Z\.md$")
        text = found[0].read_text(encoding="utf-8")
        self.assertIn(TAG, text)
        self.assertIn(f"{IMAGE}@{DIGEST}", text)

    def test_image_only_fetch_checks_the_cms_signature_and_the_pinned_signer_and_binds_no_credential(self):
        """Task T066, US4-AS8, NFR-013, D-059: fetch alone downloads SHA256SUMS, SHA256SUMS.p7s and the binary (no
        SHA256SUMS.minisig), verifies the p7s with one `openssl cms -verify` and asks the signer for its SHA-256; it
        sees no token, sends no Authorization header and calls no docker."""
        flow = ImageFlow(self, build_files=False)
        result = flow.step("fetch")
        self.assertEqual(flow.calls("docker"), [])
        self.assertFalse(any("authorization" in r["headers"] for r in flow.fake.requests))
        names = [r["path"].rsplit("/", 1)[-1] for r in flow.fake.requests]
        for name in ("SHA256SUMS", "SHA256SUMS.p7s", "venom.linux-amd64"):
            self.assertIn(name, names)
        self.assertNotIn("SHA256SUMS.minisig", names)
        verify = [c.argv for c in flow.calls("openssl") if c.argv[:2] == ["cms", "-verify"]]
        self.assertEqual(len(verify), 1)
        shown = [a.replace("\\", "/") for a in verify[0]]
        self.assertTrue(shown[shown.index("-in") + 1].endswith("/release/SHA256SUMS.p7s"), shown)
        self.assertTrue(shown[shown.index("-content") + 1].endswith("/release/SHA256SUMS"), shown)
        self.assertEqual([c.argv[0] for c in flow.calls("openssl")].count("x509"), 1)
        for call in flow.calls("curl") + flow.calls("git") + flow.calls("openssl"):
            self.assertEqual(call.secret_names(), [], call.argv)
        self.assertNotIn(flow.token, result.output())

    def test_a_bad_signature_another_signer_or_a_missing_p7s_leaves_fetch_refusing_and_build_with_it(self):
        """Task T066, US4-AS8, D-059: a p7s that does not verify says `signature`, a valid one by another certificate
        says `signer`, an altered download says `signature`, a release without the p7s says which file cannot be
        downloaded; each leaves build refusing."""
        other = sha256_of(b"another certificate")
        self.assert_fetch_refused(ImageFlow(self, build_files=False, STUB_CMS_FAIL="1"), "signature")
        self.assert_fetch_refused(ImageFlow(self, build_files=False, STUB_SIGNER_SHA256=other), "signer")
        altered = ImageFlow(self, build_files=False)
        altered.fake.altered.add("SHA256SUMS.p7s")
        self.assert_fetch_refused(altered, "signature")
        missing = ImageFlow(self, build_files=False)
        missing.fake.assets = {i: a for i, a in missing.fake.assets.items() if a["name"] != "SHA256SUMS.p7s"}
        self.assert_fetch_refused(missing, "SHA256SUMS.p7s")

    def test_fetch_stops_without_the_certificate_file_and_leaves_build_refusing(self):
        """Task T066, D-059 item 5: jenkins/release-certificate.json is mandatory; without it there is no pinned signer to
        compare with, so fetch names the certificate and nothing is built."""
        flow = ImageFlow(self, build_files=False)
        (flow.tree.root / "jenkins" / "release-certificate.json").unlink()
        self.assert_fetch_refused(flow, "certificate")

    def assert_fetch_refused(self, flow: ImageFlow, reason: str, **env) -> None:
        refused = flow.run("fetch", **env)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn(reason, refused.stderr)
        self.assertEqual(flow.run("build").returncode, 1)
        self.assertNotIn("build", flow.verbs())

    def test_image_only_stops_when_the_deadline_is_spent(self):
        """Task T066, US4-AS8, design 8: with RELEASE_DEADLINE in the past no command starts; nothing is requested."""
        flow = ImageFlow(self, build_files=False)
        flow.fake.requests.clear()
        for command in ("fetch", "check-tag", "build", "push", "verify", "latest", "smoke", "files"):
            with self.subTest(command=command):
                refused = flow.run(command, RELEASE_DEADLINE="1")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn("deadline", refused.stderr)
        self.assertEqual(flow.calls("docker"), [])
        self.assertEqual(flow.fake.requests, [])

    def test_run_mode_image_only_runs_check_tag_fetch_verify_build_push_for_a_published_tag_from_the_tags_sha_and_refuses_an_unpublished_one(self):
        """Task T066, US4-AS8, open point 23: RELEASE_SHA is empty; the public tag's own SHA names the clone and the
        sha-<7> tag; a draft or unpublished tag is refused by fetch and nothing is built or pushed."""
        flow = ImageFlow(self, build_files=False)
        for command in ("check-tag", "fetch", "build", "push", "verify"):
            flow.step(command)
        verbs = flow.verbs()
        self.assertLess(verbs.index("manifest"), verbs.index("build"))
        self.assertLess(verbs.index("build"), verbs.index("push"))
        self.assertLess(verbs.index("push"), verbs.index("pull"))
        self.assertIn(f"{IMAGE}:sha-{flow.sha[:7]}", [c.argv[1] for c in flow.docker("push")])
        draft = ImageFlow(self, build_files=False, release="draft")
        draft.step("check-tag")
        refused = draft.run("fetch")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("published", refused.stderr)
        self.assertEqual(draft.run("build").returncode, 1)
        self.assertTrue(draft.registry_untouched(), draft.verbs())

    # ---- T067: smoke ----

    def test_smoke_runs_docker_on_the_quickstart_example_and_requires_exit_0_and_final_status_pass(self):
        """Task T067, US4-AS3, SC-003: create, cp of the example into /workdir/tests/, start -a; PASS and status 0."""
        flow = self.flow
        flow.step("fetch")
        self.assertIn("smoke: PASS", flow.step("smoke").stdout)
        verbs = flow.verbs()
        self.assertEqual(verbs, ["create", "cp", "start", "rm"])
        self.assertEqual(flow.docker("create")[0].argv, ["create", f"{IMAGE}:sha-{flow.sha[:7]}"])
        copy =flow.docker("cp")[0].argv
        self.assertEqual(Path(copy[1]).name, "quickstart.yml")
        self.assertTrue(copy[2].startswith(f"{CONTAINER}:/workdir/tests/"), copy)
        self.assertEqual(flow.docker("start")[0].argv, ["start", "-a", CONTAINER])
        planted = (flow.tree.log_dir / "in-container" / "quickstart.yml").read_text(encoding="utf-8")
        self.assertEqual(planted, EXAMPLE)

    def test_smoke_fails_on_exit_1_or_a_missing_pass_line(self):
        """Task T067, US4-AS3: exit 1 with a PASS line, exit 0 with FAIL, exit 0 with no verdict all fail."""
        cases = [("1", "final status: PASS"), ("0", "final status: FAIL"), ("0", "ran 1 test, no verdict")]
        for code, out in cases:
            with self.subTest(code=code, out=out):
                flow = ImageFlow(self, STUB_DOCKER_START_RC=code, STUB_DOCKER_START_OUT=out)
                flow.step("fetch")
                refused = flow.run("smoke")
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertNotIn("smoke: PASS", refused.stdout)

    def test_smoke_and_files_use_the_local_sha_tag_and_run_before_any_push(self):
        """Task T078 (owner decision): <image>:<tag> exists only after verify, so smoke and files act on the locally
        built <image>:sha-<7>, which build created first; a failing check therefore stops the run before any push."""
        flow = self.flow
        flow.through("build")
        flow.step("smoke")
        flow.step("files")
        local = f"{IMAGE}:sha-{flow.sha[:7]}"
        verbs = flow.verbs()
        self.assertNotIn("push", verbs)
        built = flow.docker("build")
        self.assertEqual(len(built), 1)
        self.assertIn(local, built[0].argv)
        self.assertLess(verbs.index("build"), verbs.index("create"))
        self.assertEqual([c.argv for c in flow.docker("create")], [["create", local]] * 2)
        for call in flow.calls("docker"):
            self.assertNotIn(f"{IMAGE}:{TAG}", call.argv, call.argv)

    def test_smoke_uses_no_host_bind_mount(self):
        """Task T067, US4-AS3: no docker run, no -v, --volume or --mount: a host path is not the daemon's path."""
        flow = self.flow
        flow.step("fetch")
        flow.step("smoke")
        self.assertNotIn("run", flow.verbs())
        for call in flow.calls("docker"):
            for arg in call.argv:
                self.assertNotIn(arg, ("-v", "--volume", "--mount"), call.argv)
                self.assertFalse(arg.startswith(("--volume=", "--mount=", "-v=")), call.argv)


if __name__ == "__main__":
    unittest.main()
