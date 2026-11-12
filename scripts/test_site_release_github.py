#!/usr/bin/env python3
"""Tests of jenkins/release-github.sh (feature 011, tasks T014 and T030 to T034: the GitHub commands against a
fake GitHub API).
ReleaseClaim runs the script against FakeGitHub (scripts/site_release_fixture.py), a loopback HTTP server that keeps
refs, annotated tags, releases and assets, records every request and checks the Authorization header against a canary.

Contract these tests fix:
- `bash jenkins/release-github.sh <command>`; the token is one line on standard input; parameters are environment
  variables: RELEASE_API_BASE, RELEASE_REPOSITORY (owner/name), RELEASE_TAG, RELEASE_SHA, RELEASE_NOTES, JOB_NAME,
  BUILD_NUMBER, RELEASE_WORK_ROOT, RELEASE_PUBLIC_URL. A refusal exits 1 with the reason on standard error.
- `claim` writes <work>/claim.flag (lines `tag=<tag>` and `run=<JOB_NAME>#<BUILD_NUMBER>`) before it creates the
  annotated tag through the API (git/tags then git/refs), removes the flag when the create is refused, and puts the
  release notes and the trailer line `Release-Run: <JOB_NAME>#<BUILD_NUMBER>` in the tag message.
- `cleanup` does nothing without the flag; with it, it deletes the draft release and the tag only when the tag's trailer
  names this run, leaves a tag of another run (status 0), and refuses (status 1) once the release is published.
- `publish` sets draft=false through PATCH on the release and removes the flag only after the API shows the release is
  no longer a draft.
Skipped, with the reason printed, when a tool or the private paths are missing. Offline, standard library only.
"""
import hashlib
import json
import re
import unittest
from pathlib import Path

from site_release_fixture import (EXPECTED_NAMES, JOB, REPOSITORY, TAG, FakeGitHub, ReleaseCase, canary,
                                  requires_release)

NOTES = "Notes of the release, line one."
OTHER_RUN_MESSAGE = NOTES + "\n\nRelease-Run: venom/venom-release#99"


@requires_release
class ReleaseClaim(ReleaseCase):
    """Task T014: claim, cleanup and the claim flag against the fake API (US1-AS8, NFR-012, FR-006)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGitHub().start()
        self.addCleanup(self.fake.stop)
        self.sha = self.tree.sha_tip
        self.fake.seed_head("master", self.sha)
        self.env = {"RELEASE_API_BASE": self.fake.base, "RELEASE_REPOSITORY": self.fake.repository,
                    "RELEASE_SHA": self.sha, "RELEASE_NOTES": NOTES}

    def github(self, command: str, build: str = "1", tree=None, **extra):
        tree = tree or self.tree
        env = {**self.env, "BUILD_NUMBER": build, **extra}
        return tree.run(command, env=env, stdin=self.fake.token + "\n", script="release-github.sh")

    def claimed(self, build: str = "1"):
        result = self.github("claim", build)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def tag_message(self) -> str:
        return self.fake.tag_objects[self.fake.refs[f"refs/tags/{TAG}"]]["message"]

    def test_claim_creates_an_annotated_tag_carrying_the_notes_and_the_run_trailer(self):
        """Task T014, US1-AS8: an annotated tag at the SHA, with the notes and `Release-Run: <job>#<build>`."""
        self.claimed()
        sha = self.fake.refs[f"refs/tags/{TAG}"]
        self.assertIn(sha, self.fake.tag_objects, "the tag ref does not point to a tag object (lightweight tag)")
        self.assertEqual(self.fake.tag_objects[sha]["object"], self.sha)
        self.assertIn(NOTES, self.tag_message())
        self.assertRegex(self.tag_message(), rf"(?m)^Release-Run: {re.escape(JOB)}#1$")
        for request in self.fake.requests:
            self.assertIn(self.fake.token, request["headers"].get("authorization", ""))

    def test_a_second_claim_on_the_same_tag_fails_and_changes_nothing(self):
        """Task T014, US1-AS8, NFR-012: the atomic create decides; the first run's tag and trailer are untouched."""
        self.claimed("1")
        before = self.fake.snapshot()
        second = self.github("claim", "2")
        self.assertEqual(second.returncode, 1, second.stdout)
        self.assertEqual(self.fake.snapshot(), before)
        self.assertIn("#1", self.tag_message())

    def test_cleanup_deletes_the_draft_and_the_tag_of_its_own_run_only(self):
        """Task T014, US1-AS8: this run's draft and tag go; a tag and a draft of another run stay."""
        self.claimed("1")
        self.fake.seed_release(TAG, draft=True)
        other_tag = "v1.4.0-pluries.9"
        self.fake.seed_tag(other_tag, self.sha, OTHER_RUN_MESSAGE)
        other = self.fake.seed_release(other_tag, draft=True)
        result = self.github("cleanup", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"refs/tags/{TAG}", self.fake.refs)
        self.assertEqual([r["tag_name"] for r in self.fake.releases.values()], [other_tag])
        self.assertIn(f"refs/tags/{other_tag}", self.fake.refs)
        self.assertIn(other["id"], self.fake.releases)

    def test_cleanup_leaves_a_tag_whose_trailer_names_another_run(self):
        """Task T014, NFR-012: a flag in this run's directory but a tag claimed by run 99 deletes nothing."""
        self.claimed("99")
        self.fake.seed_release(TAG, draft=True)
        flag_dir = self.tree.work_dir("99")
        flag_dir.rename(flag_dir.parent / "1")
        before = self.fake.snapshot()
        result = self.github("cleanup", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual([r for r in self.fake.mutations() if r["method"] == "DELETE"], [])

    def test_cleanup_refuses_once_the_release_is_published(self):
        """Task T014, FR-006: a published release is never deleted by cleanup; status 1 and it says why."""
        self.claimed("1")
        self.fake.seed_release(TAG, draft=False)
        before = self.fake.snapshot()
        result = self.github("cleanup", "1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("published", result.stderr.lower())
        self.assertEqual(self.fake.snapshot(), before)

    def test_claim_writes_the_flag_file_before_the_create_call(self):
        """Task T014, NFR-012: at the moment the ref create arrives, claim.flag already holds the tag and the run."""
        seen = []
        self.fake.on("create_ref", lambda data: seen.append(self.tree.flags()))
        self.claimed()
        self.assertEqual(len(seen), 1, "the ref was created more or less than once")
        self.assertEqual(len(seen[0]), 1, "no flag file existed when the create call arrived")
        text = seen[0][0].read_text(encoding="utf-8")
        self.assertIn(f"tag={TAG}", text)
        self.assertIn(f"run={JOB}#1", text)

    def test_claim_runs_the_body_leak_check_on_the_notes_before_the_tag_is_created(self):
        """Review finding 5: notes naming a private path stop the claim: exit 1, no tag request, no claim flag."""
        planted = "/" + "home" + "/builder/venom/src"
        result = self.github("claim", RELEASE_NOTES=f"Notes\n\nbuilt in {planted} on an agent\n")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("leak", result.stderr.lower())
        self.assertEqual([r for r in self.fake.requests if "/git/" in r["path"]], [])
        self.assertEqual(self.fake.mutations(), [])
        self.assertEqual(self.tree.flags(), [])

    def test_a_refused_claim_422_leaves_no_flag_file(self):
        """Task T014, NFR-012: the tag is not this run's, so the refused run keeps no flag."""
        self.claimed("1")
        second = self.github("claim", "2")
        self.assertEqual(second.returncode, 1, second.stdout)
        self.assertEqual(self.tree.flags("2"), [])
        self.assertEqual(len(self.tree.flags("1")), 1, "the first run's flag was touched")

    def published_scenario(self, ignore_publish: bool):
        """A claimed tag with a draft release; the API may ignore the publish call (it stays a draft)."""
        fake = FakeGitHub().start()
        self.addCleanup(fake.stop)
        tree = self.new_tree()
        tree.base_env["RELEASE_SHA"] = tree.sha_tip
        tree.public_tag(TAG, annotated=True)
        fake.seed_head("master", tree.sha_tip)
        fake.ignore_publish = ignore_publish
        env = {"RELEASE_API_BASE": fake.base, "RELEASE_REPOSITORY": fake.repository, "RELEASE_NOTES": NOTES}
        run = lambda command: tree.run(command, env=env, stdin=fake.token + "\n", script="release-github.sh")  # noqa: E731
        self.assertEqual(run("claim").returncode, 0)
        fake.seed_release(TAG, draft=True)
        return fake, tree, run

    def test_a_flag_file_that_survives_a_published_release_makes_cleanup_refuse_and_change_nothing(self):
        """Task T014, FR-006: with the flag still there and the release published, cleanup sends no write at all."""
        self.claimed("1")
        self.fake.seed_release(TAG, draft=False)
        sent = len(self.fake.requests)
        before = self.fake.snapshot()
        result = self.github("cleanup", "1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.fake.snapshot(), before)
        self.assertEqual(self.fake.mutations(sent), [], "cleanup wrote to the API of a published release")
        self.assertEqual(len(self.tree.flags("1")), 1)


    def test_publish_removes_the_flag_file_only_after_the_api_confirms_the_release_is_not_a_draft(self):
        """Task T014, FR-006 (depends on publish of T040): the flag outlives the PATCH and a publish the API ignored."""
        fake, tree, run = self.published_scenario(ignore_publish=False)
        flag_at_patch = []
        fake.on("patch_release", lambda data: flag_at_patch.append(bool(tree.flags())))
        result = run("publish")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(flag_at_patch, [True], "the flag was removed before the publish call")
        self.assertFalse([r for r in fake.releases.values() if r["draft"]], "the release is still a draft")
        self.assertEqual(tree.flags(), [], "the flag survived a confirmed publish")
        _fake, tree_ignored, run_ignored = self.published_scenario(ignore_publish=True)
        refused = run_ignored("publish")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertEqual(len(tree_ignored.flags()), 1, "the flag was removed although the release is still a draft")


def named(name: str, text: str) -> bool:
    """True when the file name appears in the text and is not just the prefix of a longer name."""
    return re.search(re.escape(name) + r"(?![\w-])", text) is not None


def draft_flag(request: dict):
    """The `draft` member of a JSON request body, or None when the body is not JSON or has no such member."""
    if request["method"] not in ("POST", "PATCH") or "/assets" in request["path"]:
        return None
    try:
        return json.loads(request["body"]).get("draft")
    except (ValueError, AttributeError):
        return None


def patch_data(request: dict) -> dict:
    return json.loads(request["body"]) if request["method"] == "PATCH" and request["body"] else {}


def files_holding(roots: list, value: str) -> list:
    needle = value.encode()
    return [p for root in roots for p in Path(root).rglob("*") if p.is_file() and needle in p.read_bytes()]


class Flow:
    """A release in the making: a throwaway tree whose bare public repository holds the claimed tag at the tip of its
    master, a FakeGitHub, the fifteen release files in the work directory, the pinned signer certificate and the token
    on standard input.
    Contract of release-github.sh, besides the one of ReleaseClaim: every command reads the token on standard input;
    the files are <work>/dist/*, <work>/src/LICENSE and <work>/src/NOTICE; jenkins/release-certificate.json (beside
    the script) pins the signer (D-059: minisign and RELEASE_PUBKEY are gone);
    RELEASE_DOWNLOAD_BASE the anonymous download host (default https://github.com, loopback allowed, the URL is
    <base>/<repository>/releases/download/<tag>/<name>); the draft, upload, verify-draft, publish, verify-public and
    withdraw commands find the release by its tag; uploads go to the upload path of the configured repository."""

    def __init__(self, case: ReleaseCase, **config):
        self.case = case
        self.tree = case.new_tree(**config)
        self.fake = FakeGitHub().start()
        case.addCleanup(self.fake.stop)
        self.sha = self.tree.sha_tip
        self.fake.seed_head("master", self.sha)
        self.tree.public_tag(TAG, annotated=True)
        self.assets = self.tree.prepare_release_files()
        self.tree.write_certificate()
        self.env = {"RELEASE_API_BASE": self.fake.base, "RELEASE_DOWNLOAD_BASE": self.fake.base,
                    "RELEASE_REPOSITORY": self.fake.repository, "RELEASE_SHA": self.sha, "RELEASE_NOTES": NOTES}

    def run(self, command: str, *args: str, token: str | None = None, flags=(), **extra):
        stdin = (token or self.fake.token) + "\n"
        return self.tree.run(command, list(args), env={**self.env, **extra}, stdin=stdin, script="release-github.sh",
                             bash_flags=flags)

    def step(self, command: str, *args: str, **kwargs):
        result = self.run(command, *args, **kwargs)
        self.case.assertEqual(result.returncode, 0, f"{command}: {result.stderr[-600:]}")
        return result

    def claim_draft(self) -> dict:
        """The tag is claimed through the script; the draft release is the one the draft command would make."""
        self.step("claim")
        return self.fake.seed_release(TAG, draft=True)

    def release(self, tag: str = TAG) -> dict:
        found = [r for r in self.fake.releases.values() if r["tag_name"] == tag]
        self.case.assertEqual(len(found), 1, f"releases for {tag}: {found}")
        return found[0]

    def record_input(self) -> str:
        path = self.tree.work_dir() / "record-input"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def whole(self, flags=()) -> list:
        """claim, draft, upload, verify-draft, publish, verify-public and withdraw, each under the given bash flags."""
        results = [self.step(command, *args, flags=flags)
                   for command, args in (("claim", ()), ("draft", ()), ("upload", ()), ("verify-draft", ()),
                                         ("publish", ()), ("verify-public", ()), ("withdraw", ("rehearsal",)))]
        return results


@requires_release
class ReleaseDraft(ReleaseCase):
    """Task T030: draft, upload, verify-draft and publish against the fake API (US2-AS3, AS6, FR-004, FR-006).
    Contract: `draft` creates the draft release for the tag (body RELEASE_NOTES); `upload` sends the fifteen files (D-059: the ten executables, SHA256SUMS, SHA256SUMS.p7s, SHA256SUMS.p7s.tsr, LICENSE, NOTICE);
    `verify-draft` downloads each asset through the API and compares its SHA-256 with the file in the work directory
    (a difference exits 1 and names the file; nothing is written); `publish` runs check-pinned, reads the latest
    release, writes `previous_latest=<tag|none>` to record-input, then PATCHes draft=false with make_latest "true" when
    the tag is greater than the previous latest (by version, numbers compared as numbers) and "false" otherwise."""

    def setUp(self):
        super().setUp()
        self.flow = Flow(self)

    def test_the_release_is_a_draft_until_every_file_is_uploaded_and_checked(self):
        """Task T030, US2-AS6, FR-006: a draft through upload and verify-draft; draft=false only in publish, last."""
        flow = self.flow
        flow.step("claim")
        flow.step("draft")
        release = flow.release()
        self.assertTrue(release["draft"])
        flow.step("upload")
        flow.step("verify-draft")
        self.assertTrue(release["draft"], "the release left the draft state before publish")
        flow.step("publish")
        self.assertFalse(release["draft"])
        flags = [(i, draft_flag(r)) for i, r in enumerate(flow.fake.requests) if draft_flag(r) is not None]
        self.assertEqual([f for _, f in flags], [True, False], "draft must be true once and false once")
        uploads = [i for i, r in enumerate(flow.fake.requests) if r["method"] == "POST" and "/assets" in r["path"]]
        self.assertEqual(len(uploads), 15)
        self.assertGreater(flags[1][0], max(uploads))

    def test_the_assets_are_the_ten_files_sha256sums_the_p7s_its_timestamp_the_licence_and_the_notice(self):
        """Task T030, T079, US2-AS3, FR-004, NFR-008, D-059: fifteen assets, each with the bytes of its file; no
        SHA256SUMS.minisig."""
        flow = self.flow
        flow.step("claim")
        flow.step("draft")
        flow.step("upload")
        uploaded = {a["name"]: a["data"] for a in flow.fake.assets.values()}
        self.assertEqual(sorted(uploaded), sorted([*EXPECTED_NAMES, "SHA256SUMS", "SHA256SUMS.p7s",
                                          "SHA256SUMS.p7s.tsr", "LICENSE", "NOTICE"]))
        self.assertEqual(uploaded, flow.assets)

    def test_an_upload_that_fails_halfway_leaves_no_published_release_and_cleanup_removes_the_draft_and_the_tag_it_made(self):
        """Task T030, US2-AS6, FR-006: the fifth upload fails; nothing is published; cleanup deletes draft and tag."""
        flow = self.flow
        flow.fake.fail_upload_number = 5
        flow.step("claim")
        flow.step("draft")
        failed = flow.run("upload")
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertTrue(flow.release()["draft"])
        self.assertLess(len(flow.fake.assets), 15)
        self.assertNotIn(False, [draft_flag(r) for r in flow.fake.requests])
        flow.step("cleanup")
        self.assertEqual(flow.fake.releases, {})
        self.assertNotIn(f"refs/tags/{TAG}", flow.fake.refs)

    def test_upload_refuses_when_the_clone_holds_no_notice_and_sends_nothing(self):
        """Task T079, NFR-008: the NOTICE of the clone is a release file like LICENSE; without it upload stops, names it
        and no asset is sent."""
        flow = self.flow
        flow.step("claim")
        flow.step("draft")
        (flow.tree.work_dir() / "src" / "NOTICE").unlink()
        failed = flow.run("upload")
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertIn("NOTICE", failed.stderr)
        self.assertEqual(flow.fake.assets, {})

    def test_verify_draft_names_an_uploaded_notice_that_differs_from_the_file_of_the_clone(self):
        """Task T079, NFR-008: NOTICE is compared like the other fourteen assets."""
        flow = self.flow
        flow.step("claim")
        flow.step("draft")
        flow.step("upload")
        flow.fake.altered.add("NOTICE")
        result = flow.run("verify-draft")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named("NOTICE", result.stderr), result.stderr)
        self.assertFalse(named("LICENSE", result.stderr), "an intact file was named")

    def test_an_uploaded_file_that_differs_from_the_recorded_hash_stops_before_publish(self):
        """Task T030, US2-AS6: a draft asset that does not match its file fails verify-draft, names it, writes nothing."""
        flow = self.flow
        flow.step("claim")
        flow.step("draft")
        flow.step("upload")
        flow.fake.altered.add("venom.linux-arm")
        sent = len(flow.fake.requests)
        result = flow.run("verify-draft")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named("venom.linux-arm", result.stderr), result.stderr)
        self.assertFalse(named("venom.linux-386", result.stderr), "an intact file was named")
        self.assertEqual(flow.fake.mutations(sent), [])
        self.assertTrue(flow.release()["draft"])

    def test_publish_reads_the_current_latest_before_publishing_and_writes_it_to_the_record_as_previous_latest(self):
        """Task T030, FR-006, open point 28: previous_latest is read before the publish call, `none` when there is none."""
        for previous, expected in (("v1.3.0-pluries.1", "v1.3.0-pluries.1"), (None, "none")):
            with self.subTest(previous=previous):
                flow = Flow(self)
                if previous:
                    flow.fake.seed_published(previous, {})
                flow.claim_draft()
                flow.step("publish")
                self.assertRegex(flow.record_input(), rf"(?m)^previous_latest={re.escape(expected)}$")
                paths = [(r["method"], r["path"]) for r in flow.fake.requests]
                read = [i for i, (m, p) in enumerate(paths) if m == "GET" and p.endswith("/releases/latest")]
                published = [i for i, r in enumerate(flow.fake.requests) if draft_flag(r) is False]
                self.assertTrue(read and published)
                self.assertLess(read[0], published[0], "the latest release was read after the publish call")

    def test_publish_sets_make_latest_only_when_the_tag_is_greater_than_the_previous_latest(self):
        """Task T030, FR-006: make_latest is "true" above the previous latest, "false" below it, numbers as numbers."""
        cases = [(None, "true"), ("v1.3.0-pluries.7", "true"), ("v1.4.0-pluries.1", "true"),
                 ("v1.4.0-pluries.10", "false"), ("v1.10.0-pluries.1", "false")]
        for previous, expected in cases:
            with self.subTest(previous=previous):
                flow = Flow(self)
                if previous:
                    flow.fake.seed_published(previous, {})
                flow.claim_draft()
                flow.step("publish")
                patches = [patch_data(r) for r in flow.fake.requests if draft_flag(r) is False]
                self.assertEqual([p.get("make_latest") for p in patches], [expected])


@requires_release
class ReleaseGithubSecrets(ReleaseCase):
    """Task T031: the token reaches GitHub only in the Authorization header (US2-AS5, NFR-001, NFR-003, NFR-013)."""

    def setUp(self):
        super().setUp()
        self.flow = Flow(self)

    def test_the_token_is_in_no_argv_no_child_environment_and_no_log_even_under_bash_x(self):
        """Task T031, NFR-001: the whole flow under `bash -x`; the fake server sees the canary in the header of every
        API request, and no argv, environment, output or file of the run holds it."""
        flow = self.flow
        results = flow.whole(flags=("-x",))
        token = flow.fake.token
        for result in results:
            self.assertNotIn(token, result.output())
        for stub in ("curl", "git"):
            for call in results[-1].calls(stub):
                self.assertNotIn(token, "\n".join(call.argv + list(call.env.values())), stub)
        api = [r for r in flow.fake.requests if r["path"].startswith(("/repos/", "/uploads/"))]
        self.assertTrue(api)
        for request in api:
            self.assertIn(token, request["headers"].get("authorization", ""))
        self.assertEqual(files_holding([flow.tree.work_root, flow.tree.root], token), [])

    def anonymous_and_repository_only(self, flow) -> list:
        repository = flow.fake.repository
        allowed = (f"/repos/{repository}/", f"/uploads/repos/{repository}/", f"/{repository}/releases/download/")
        return [r["path"] for r in flow.fake.requests if not r["path"].startswith(allowed)]

    def test_every_request_goes_to_the_configured_repository_only(self):
        """Task T031, NFR-003: only the configured repository's API, upload and download paths are used; urls inside
        responses are not followed (a decoy host receives nothing); a base other than GitHub or loopback is refused."""
        flow = self.flow
        flow.whole()
        self.assertEqual(self.anonymous_and_repository_only(flow), [])
        downloads = [r for r in flow.fake.requests if "/releases/download/" in r["path"]]
        self.assertTrue(downloads)
        self.assertEqual([r for r in downloads if "authorization" in r["headers"]], [], "a public download sent the token")
        decoy_flow = Flow(self)
        decoy = FakeGitHub().start()
        self.addCleanup(decoy.stop)
        decoy_flow.fake.url_base = decoy.base
        for command in ("claim", "draft", "upload", "verify-draft"):
            decoy_flow.step(command)
        self.assertEqual(decoy.requests, [], "the script followed a url taken from a response")
        for base in ("https://evil.example", "http://127.0.0.1.evil.example", "https://api.github.com.evil.example"):
            with self.subTest(base=base):
                refused = self.flow.run("claim", RELEASE_API_BASE=base)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertNotIn(self.flow.fake.token, refused.output())

    def test_a_failed_run_prints_no_token(self):
        """Task T031, NFR-001: a refused token, a failed upload and a refused base print no token, wrong or right."""
        wrong = canary("wrong", "31")
        flow = self.flow
        bad = flow.run("claim", token=wrong, flags=("-x",))
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertNotIn(wrong, bad.output())
        flow.fake.fail_upload_number = 2
        flow.step("claim")
        flow.step("draft")
        failed = flow.run("upload", flags=("-x",))
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertNotIn(flow.fake.token, failed.output())
        refused = flow.run("draft", flags=("-x",), RELEASE_API_BASE="https://evil.example")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertNotIn(flow.fake.token, refused.output())


@requires_release
class ReleasePinned(ReleaseCase):
    """Task T032: check-pinned <tag> <sha> reads the public repository (RELEASE_PUBLIC_URL) and stops when its master
    tip is not the SHA or when the tag does not peel to the SHA (US2-AS11, FR-010); publish runs it first."""

    def pinned(self, tree, sha: str):
        return tree.run("check-pinned", [TAG, sha], env={"RELEASE_REPOSITORY": REPOSITORY})

    def test_check_pinned_passes_when_master_and_the_tag_point_at_the_sha(self):
        """Task T032, US2-AS11: an annotated and a lightweight tag at the tip pass."""
        for annotated in (True, False):
            with self.subTest(annotated=annotated):
                tree = self.new_tree()
                tree.public_tag(TAG, annotated=annotated)
                result = self.pinned(tree, tree.sha_tip)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_stops_when_master_moved(self):
        """Task T032, US2-AS11, FR-010: the approved SHA is older than the master tip; the message says master."""
        tree = self.new_tree()
        tree.public_tag(TAG, annotated=True, commit=tree.sha_first)
        result = self.pinned(tree, tree.sha_first)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("master", result.stderr.lower())

    def test_it_stops_when_the_tag_peels_to_another_commit(self):
        """Task T032, FR-010: master is at the SHA but the (annotated or lightweight) tag is elsewhere, or missing."""
        for annotated in (True, False, None):
            with self.subTest(annotated=annotated):
                tree = self.new_tree()
                if annotated is not None:
                    tree.public_tag(TAG, annotated=annotated, commit=tree.sha_first)
                result = self.pinned(tree, tree.sha_tip)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(TAG, result.stderr)

    def test_a_public_url_that_does_not_end_with_the_repository_is_refused_naming_both_variables(self):
        """Review finding 8: RELEASE_PUBLIC_URL and RELEASE_REPOSITORY name one repository; when they disagree,
        check-pinned and publish stop (exit 1, both names in the message) before any API request."""
        other = "pluries-example/another-repository"
        tree = self.new_tree()
        result = tree.run("check-pinned", [TAG, tree.sha_tip], env={"RELEASE_REPOSITORY": other})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RELEASE_PUBLIC_URL", result.stderr)
        self.assertIn("RELEASE_REPOSITORY", result.stderr)
        flow = Flow(self)
        flow.claim_draft()
        sent = len(flow.fake.requests)
        published = flow.run("publish", RELEASE_REPOSITORY=other)
        self.assertEqual(published.returncode, 1, published.stdout)
        self.assertIn("RELEASE_PUBLIC_URL", published.stderr)
        self.assertIn("RELEASE_REPOSITORY", published.stderr)
        self.assertEqual(flow.fake.requests[sent:], [])

    def test_publish_runs_check_pinned_first(self):
        """Task T032, US2-AS11, FR-010: with master moved, publish exits 1 before it sends any request to the API."""
        flow = Flow(self)
        flow.claim_draft()
        sent = len(flow.fake.requests)
        result = flow.run("publish", RELEASE_SHA=flow.tree.sha_first)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("master", result.stderr.lower())
        self.assertEqual(flow.fake.requests[sent:], [], "publish called the API before check-pinned")
        self.assertTrue(flow.release()["draft"])


def public_flow(case: ReleaseCase, **config) -> Flow:
    """A flow whose release is already published with its fifteen assets; record-input holds previous_latest=none."""
    flow = Flow(case, **config)
    flow.fake.seed_published(TAG, flow.assets, body=NOTES)
    flow.tree.write_record_input(previous_latest="none")
    return flow


@requires_release
class ReleaseVerifyPublic(ReleaseCase):
    """Task T033: verify-public downloads the public assets without the token, compares each with the file in the work
    directory, verifies the downloaded SHA256SUMS against the downloaded SHA256SUMS.p7s (D-059: `openssl cms -verify`
    and the signer pinned in jenkins/release-certificate.json; no minisign) and on any difference, a bad signature
    or another signer writes `incident=<text naming the file, the signature or the signer>` to record-input and runs
    the withdraw of ReleaseWithdraw."""

    def withdrawn(self, flow) -> None:
        release = flow.release()
        self.assertTrue(release["body"].startswith("WITHDRAWN:"), release["body"][:80])
        self.assertTrue(release["prerelease"])
        self.assertTrue((flow.tree.work_dir() / "docs" / "release" / TAG / "WITHDRAWN").is_file())

    def test_matching_assets_and_signature_pass(self):
        """Task T033, US2-AS12, D-059: status 0, `public check passed`, one CMS verify of the downloaded p7s against the
        downloaded SHA256SUMS, the signer asked for its fingerprint, no write, no token sent."""
        flow = public_flow(self)
        result = flow.step("verify-public")
        self.assertIn("public check passed", result.stdout)
        checks = [c for c in result.calls("openssl") if c.argv[:2] == ["cms", "-verify"]]
        self.assertEqual(len(checks), 1)
        shown = [a.replace("\\", "/") for a in checks[0].argv]
        self.assertEqual(shown[shown.index("-inform") + 1], "DER")
        self.assertTrue(shown[shown.index("-in") + 1].endswith("/public/SHA256SUMS.p7s"), shown)
        self.assertTrue(shown[shown.index("-content") + 1].endswith("/public/SHA256SUMS"), shown)
        self.assertEqual([c.argv[0] for c in result.calls("openssl")].count("x509"), 1)
        self.assertEqual(flow.fake.mutations(), [])
        self.assertEqual([r for r in flow.fake.requests if "authorization" in r["headers"]], [])

    def test_an_altered_public_asset_records_an_incident_and_withdraws_the_release(self):
        """Task T033, US2-AS12, FR-011: a public asset that differs from its file: exit 1, named, incident, withdrawn."""
        flow = public_flow(self)
        flow.fake.altered.add("venom.linux-arm")
        result = flow.run("verify-public")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named("venom.linux-arm", result.stderr), result.stderr)
        self.assertRegex(flow.record_input(), r"(?m)^incident=.*venom\.linux-arm")
        self.withdrawn(flow)

    def test_an_altered_public_notice_records_an_incident_and_withdraws_the_release(self):
        """Task T079, NFR-008, FR-011: NOTICE is one of the public assets compared with the file of the clone."""
        flow = public_flow(self)
        flow.fake.altered.add("NOTICE")
        result = flow.run("verify-public")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named("NOTICE", result.stderr), result.stderr)
        self.assertRegex(flow.record_input(), r"(?m)^incident=.*NOTICE")
        self.withdrawn(flow)

    def test_an_altered_public_signature_or_timestamp_file_records_an_incident_and_withdraws_the_release(self):
        """Task T033, D-059, FR-011: SHA256SUMS.p7s and SHA256SUMS.p7s.tsr are public assets compared with the files of
        the work directory like the others; a changed one is named and withdraws the release."""
        for name in ("SHA256SUMS.p7s", "SHA256SUMS.p7s.tsr"):
            with self.subTest(asset=name):
                flow = public_flow(self)
                flow.fake.altered.add(name)
                result = flow.run("verify-public")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, rf"(?m)^release: public check: {re.escape(name)} differs")
                self.assertRegex(flow.record_input(), rf"(?m)^incident=.*{re.escape(name)} differs")
                self.withdrawn(flow)

    def test_an_invalid_signature_on_the_public_assets_withdraws(self):
        """Task T033, FR-011, D-059: matching hashes but a p7s whose CMS verify fails withdraws the release too."""
        flow = public_flow(self, STUB_CMS_FAIL="1")
        result = flow.run("verify-public")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("signature", result.stderr.lower())
        self.assertRegex(flow.record_input(), r"(?m)^incident=.*signature")
        self.withdrawn(flow)

    def test_a_valid_signature_by_another_certificate_on_the_public_assets_withdraws(self):
        """Task T033, FR-011, FR-013, D-059: the CMS verify passes but the signer is not the pinned certificate: the
        incident names the signer and the release is withdrawn."""
        other = hashlib.sha256(b"another certificate").hexdigest()
        flow = public_flow(self, STUB_SIGNER_SHA256=other)
        result = flow.run("verify-public")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("signer", result.stderr.lower())
        self.assertRegex(flow.record_input(), r"(?m)^incident=.*signer")
        self.withdrawn(flow)

    def test_a_failed_download_withdraws_nothing_and_says_the_download_failed(self):
        """Review finding 2: a 5xx on the anonymous download is not proof of a bad release: no withdrawal, exit 1."""
        flow = public_flow(self)
        flow.fake.fail_download = True
        sent = len(flow.fake.requests)
        result = flow.run("verify-public")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("download", result.stderr.lower())
        self.assertNotIn("withdrawn", result.stderr.lower())
        self.assertEqual(flow.fake.mutations(sent), [])
        self.assertFalse(flow.release()["prerelease"])
        self.assertFalse(flow.release()["body"].startswith("WITHDRAWN"))
        self.assertFalse((flow.tree.work_dir() / "docs" / "release" / TAG / "WITHDRAWN").exists())


@requires_release
class ReleaseWithdraw(ReleaseCase):
    """Task T034: `withdraw "<reason>"` (US2-AS13, NFR-010, FR-014). On a published release it puts the first body line
    `WITHDRAWN: <reason>` above the old body, marks it a pre-release, writes <work>/docs/release/<tag>/WITHDRAWN (the
    tag and the reason), moves latest back to previous_latest of record-input (a PATCH with make_latest "true" on that
    release), writes `latest_after_withdrawal=<tag|unset>` to record-input and prints `no previous release` when there
    is none. A release still in draft is deleted instead."""

    REASON = "public asset differs from the signed list"

    def published(self, previous: str | None = None):
        flow = Flow(self)
        previous_release = flow.fake.seed_published(previous, {}) if previous else None
        flow.fake.seed_published(TAG, flow.assets, body=NOTES)
        flow.tree.write_record_input(previous_latest=previous or "none")
        return flow, previous_release

    def marker(self, flow) -> Path:
        return flow.tree.work_dir() / "docs" / "release" / TAG / "WITHDRAWN"

    def test_the_release_is_marked_withdrawn_with_a_first_line_and_as_a_pre_release(self):
        """Task T034, US2-AS13, NFR-010: first line, old notes kept, pre-release, not deleted, assets kept."""
        flow, _ = self.published()
        flow.step("withdraw", self.REASON)
        release = flow.release()
        lines = release["body"].splitlines()
        self.assertEqual(lines[0], f"WITHDRAWN: {self.REASON}")
        self.assertIn(NOTES, release["body"])
        self.assertTrue(release["prerelease"])
        self.assertFalse(release["draft"])
        self.assertEqual(len([a for a in flow.fake.assets.values() if a["release"] == release["id"]]), 15)

    def test_the_tag_is_written_to_the_retired_marker_and_check_tag_refuses_it_afterwards(self):
        """Task T034, NFR-010, FR-014: the marker holds the tag and the reason; check-tag then refuses the tag for good."""
        flow, _ = self.published()
        flow.step("withdraw", self.REASON)
        text = self.marker(flow).read_text(encoding="utf-8")
        self.assertIn(TAG, text)
        self.assertIn(self.REASON, text)
        records = (flow.tree.work_dir() / "docs" / "release").as_posix()
        refused = flow.tree.run("check-tag", [TAG], env={"RELEASE_RECORDS_DIR": records})
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertRegex(refused.stderr.lower(), "withdrawn|retired")
        free = flow.tree.run("check-tag", ["v1.4.0-pluries.3"], env={"RELEASE_RECORDS_DIR": records})
        self.assertEqual(free.returncode, 0, free.stderr)

    def test_latest_moves_back_to_the_previous_release(self):
        """Task T034, US2-AS13: latest was the withdrawn tag and is the previous release afterwards, by an explicit PATCH."""
        flow, previous = self.published("v1.3.0-pluries.1")
        self.assertEqual(flow.fake.latest()["tag_name"], TAG)
        flow.step("withdraw", self.REASON)
        self.assertEqual(flow.fake.latest()["tag_name"], "v1.3.0-pluries.1")
        patches = [r for r in flow.fake.requests if patch_data(r).get("make_latest") == "true"]
        self.assertEqual([p["path"].rsplit("/", 1)[-1] for p in patches], [str(previous["id"])])
        self.assertRegex(flow.record_input(), r"(?m)^latest_after_withdrawal=v1\.3\.0-pluries\.1$")

    def test_a_draft_left_over_is_deleted(self):
        """Task T034, US2-AS13: a release of the tag still in draft is deleted with its assets, the marker is written."""
        flow = Flow(self)
        flow.tree.write_record_input(previous_latest="none")
        flow.fake.seed_release(TAG, draft=True, files={"venom.linux-arm": b"partial upload\n"})
        flow.step("withdraw", self.REASON)
        self.assertEqual([r for r in flow.fake.releases.values() if r["tag_name"] == TAG], [])
        self.assertEqual(flow.fake.assets, {})
        self.assertTrue(self.marker(flow).is_file())

    def test_withdrawal_with_no_previous_release_leaves_latest_unset_and_says_so(self):
        """Task T034, NFR-010: no previous release: no make_latest "true" sent, latest unset, the output says so."""
        flow, _ = self.published()
        result = flow.step("withdraw", self.REASON)
        self.assertIn("no previous release", result.stdout.lower())
        self.assertIsNone(flow.fake.latest())
        self.assertEqual([r for r in flow.fake.requests if patch_data(r).get("make_latest") == "true"], [])
        self.assertRegex(flow.record_input(), r"(?m)^latest_after_withdrawal=unset$")

    def test_a_later_build_finds_the_previous_latest_in_the_public_list_and_moves_latest_back(self):
        """Review finding 6: with no record-input of this build, the previous latest is the highest other published,
        non-draft, non-pre-release, non-withdrawn tag of the public releases list."""
        flow = Flow(self)
        previous = flow.fake.seed_published("v1.3.0-pluries.1", {})
        flow.fake.seed_published("v1.2.0-pluries.4", {})
        withdrawn = flow.fake.seed_release("v1.9.0-pluries.1", draft=False, prerelease=True)
        withdrawn["body"] = "WITHDRAWN: bad\n\nold notes"
        flow.fake.seed_release("v2.0.0-pluries.1", draft=True)
        flow.fake.seed_published(TAG, flow.assets, body=NOTES)
        flow.tree.make_work("2")
        result = flow.run("withdraw", self.REASON, BUILD_NUMBER="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = [r for r in flow.fake.requests if patch_data(r).get("make_latest") == "true"]
        self.assertEqual([p["path"].rsplit("/", 1)[-1] for p in patches], [str(previous["id"])])
        self.assertEqual(flow.fake.latest()["tag_name"], "v1.3.0-pluries.1")


if __name__ == "__main__":
    unittest.main()
