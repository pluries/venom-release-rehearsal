#!/usr/bin/env python3
"""Tests of the reach step of the site image check (story #3680, AC-3, AC-5).
ImageReach covers the bug found by V8: on the docker-e2e agent the site container is a sibling on the shared daemon,
so its published port is not on the agent's loopback; scripts/site-image-reach.sh must fall back to the container's own
address on port 8080. curl and docker are stubs first on PATH. Offline, standard library only.
Run: python scripts/test_site_image_reach.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

BASH = shutil.which("bash")
SCRIPT = ROOT / "scripts" / "site-image-reach.sh"
NAME = "venom-site-check"
PORT = "18080"
LOOPBACK = "http://127.0.0.1:%s/" % PORT
IP = "172.17.0.5"
CONTAINER = "http://%s:8080/" % IP

CURL_STUB = """\
#!/usr/bin/env bash
url=""
for a in "$@"; do case "$a" in http*) url="$a" ;; esac; done
printf '%s\\n' "$url" >> "$STUB_LOG_DIR/curl.log"
key=$(printf '%s' "$url" | tr -c 'A-Za-z0-9' _)
n=0
[ -f "$STUB_LOG_DIR/n_$key" ] && n=$(cat "$STUB_LOG_DIR/n_$key")
echo $((n + 1)) > "$STUB_LOG_DIR/n_$key"
codes=000
IFS=,
for e in $STUB_CODES; do [ "${e%%=*}" = "$url" ] && codes="${e#*=}"; done
IFS=/ read -ra arr <<< "$codes"
i=$n
[ "$i" -ge "${#arr[@]}" ] && i=$((${#arr[@]} - 1))
code="${arr[$i]}"
printf '%s' "$code"
[ "$code" = 200 ] && exit 0
exit 7
"""

DOCKER_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/docker.log"
[ -n "${STUB_INSPECT_FAIL:-}" ] && exit 1
printf '%s' "${STUB_INSPECT-}"
exit 0
"""


def read_log(path: Path) -> list:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


@unittest.skipUnless(BASH, "bash is not installed")
class ImageReach(unittest.TestCase):
    """Bug found by V8 of story #3680, AC-3/AC-5: the sibling site container is reached by its own address."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        for name, text in (("curl", CURL_STUB), ("docker", DOCKER_STUB)):
            path = self.stubs / name
            path.write_text(text, encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def reach(self, codes, inspect=None, inspect_fail=False, tries="3"):
        env = dict(os.environ)
        env["PATH"] = str(self.stubs) + os.pathsep + env.get("PATH", "")
        env["STUB_LOG_DIR"] = self.tmp.as_posix()
        env["STUB_CODES"] = ",".join("%s=%s" % item for item in codes.items())
        env["SITE_IMAGE_TRIES"] = tries
        env["SITE_IMAGE_SLEEP"] = "0"
        env.pop("STUB_INSPECT", None)
        env.pop("STUB_INSPECT_FAIL", None)
        if inspect is not None:
            env["STUB_INSPECT"] = inspect
        if inspect_fail:
            env["STUB_INSPECT_FAIL"] = "1"
        command = "source '%s'; reach_site %s %s" % (SCRIPT.as_posix(), NAME, PORT)
        return subprocess.run([BASH, "-c", command], capture_output=True, text=True, env=env, timeout=60)

    def curl_calls(self):
        return read_log(self.tmp / "curl.log")

    def docker_calls(self):
        return read_log(self.tmp / "docker.log")

    def test_t1_loopback_answers_200(self):
        done = self.reach({LOOPBACK: "200"})
        self.assertEqual((done.returncode, done.stdout.strip()), (0, LOOPBACK), done.stderr)
        self.assertEqual(self.docker_calls(), [])
        self.assertEqual(self.curl_calls(), [LOOPBACK])

    def test_t2_container_address_answers_when_loopback_does_not(self):
        done = self.reach({LOOPBACK: "000", CONTAINER: "200"}, inspect=IP + " ")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, CONTAINER), done.stderr)
        self.assertEqual(self.curl_calls()[:2], [LOOPBACK, CONTAINER])
        self.assertTrue(any("inspect" in call and NAME in call for call in self.docker_calls()))

    def test_t3_nothing_answers(self):
        done = self.reach({LOOPBACK: "000", CONTAINER: "503"}, inspect=IP + " ", tries="3")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        for text in ("reach_site:", "503", LOOPBACK, CONTAINER):
            self.assertIn(text, done.stderr)
        self.assertEqual(self.curl_calls().count(LOOPBACK), 3)
        self.assertEqual(self.curl_calls().count(CONTAINER), 3)

    def test_t4_only_a_real_address_is_used(self):
        cases = {"fails": dict(inspect_fail=True), "empty": dict(inspect=""), "no value": dict(inspect="<no value>"),
                 "word": dict(inspect="abc"), "short": dict(inspect="1.2.3"), "long": dict(inspect="1.2.3.4.5"),
                 "mixed": dict(inspect="abc 1.2.3 ")}
        for label, kwargs in cases.items():
            with self.subTest(label):
                (self.tmp / "curl.log").unlink(missing_ok=True)
                done = self.reach({LOOPBACK: "000"}, tries="2", **kwargs)
                self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)
                self.assertEqual(set(self.curl_calls()), {LOOPBACK})
                self.assertEqual(self.curl_calls().count(LOOPBACK), 2)

    def test_t4_address_after_other_tokens_is_used(self):
        done = self.reach({LOOPBACK: "000", CONTAINER: "200"}, inspect="<no value> " + IP + " 172.18.0.9 ")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, CONTAINER), done.stderr)
        self.assertNotIn("http://172.18.0.9:8080/", self.curl_calls())

    def test_t5_a_later_round_answers(self):
        done = self.reach({LOOPBACK: "000", CONTAINER: "000/000/200"}, inspect=IP + " ", tries="5")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, CONTAINER), done.stderr)
        self.assertEqual(self.curl_calls().count(CONTAINER), 3)
        self.assertEqual(self.curl_calls().count(LOOPBACK), 3)

    def test_t5_the_bound_is_respected(self):
        done = self.reach({LOOPBACK: "000", CONTAINER: "000/000/200"}, inspect=IP + " ", tries="2")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertEqual(self.curl_calls().count(LOOPBACK), 2)

    def test_t5_syntax_and_no_side_effect_when_sourced(self):
        checked = subprocess.run([BASH, "-n", SCRIPT.as_posix()], capture_output=True, text=True)
        self.assertEqual((checked.returncode, checked.stderr), (0, ""))
        env = dict(os.environ)
        env["PATH"] = str(self.stubs) + os.pathsep + env.get("PATH", "")
        env["STUB_LOG_DIR"] = self.tmp.as_posix()
        sourced = subprocess.run([BASH, "-c", "source '%s'" % SCRIPT.as_posix()], capture_output=True, text=True,
                                 env=env, timeout=60)
        self.assertEqual((sourced.returncode, sourced.stdout, sourced.stderr), (0, "", ""))
        self.assertEqual(self.curl_calls() + self.docker_calls(), [])


if __name__ == "__main__":
    unittest.main()
