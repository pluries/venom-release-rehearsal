#!/usr/bin/env python3
"""Tests of the site image Dockerfile checks (story #3622, task #3661).
check_dockerfile returns findings, each starting with a kind prefix:
from: stages: copy: add: user: install: env: license: unsupported:
Offline, standard library only. Run: python scripts/test_site_image_docker.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_image_dockerfile as sd  # noqa: E402
from test_build_site import ROOT  # noqa: E402

DOCKERFILE = "deploy/site/Dockerfile"
D1 = "a" * 64
D2 = "b" * 64

GOOD = f"""\
FROM python@sha256:{D1} AS build
WORKDIR /src
COPY site/ site/
COPY scripts/ scripts/
COPY README.md LICENSE ./
RUN python scripts/build_site.py --out /out

FROM nginx@sha256:{D2}
COPY --from=build /out/ /usr/share/nginx/html/
COPY --from=build /src/deploy/site/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /src/LICENSE /usr/share/nginx/html/LICENSE
USER 101:101
EXPOSE 8080
"""


def read_real() -> str:
    path = ROOT / DOCKERFILE
    if not path.is_file():
        raise AssertionError(f"{DOCKERFILE} does not exist")
    return path.read_text(encoding="utf-8")


def kinds(text: str, *prefixes: str) -> list[str]:
    return [f for f in sd.check_dockerfile(text) if f.lower().startswith(tuple(p + ":" for p in prefixes))]


def with_final(extra: str) -> str:
    return GOOD.replace("EXPOSE 8080", extra + "\nEXPOSE 8080")


class T1DigestTests(unittest.TestCase):
    KINDS = ("from",)

    def test_T1_compliant_has_no_from_finding(self):
        self.assertEqual(kinds(GOOD, *self.KINDS), [])

    def test_T1_alias_after_digest_is_accepted(self):
        self.assertEqual(kinds(GOOD.replace(f"@sha256:{D1} AS build", f"@sha256:{D1} as build"), *self.KINDS), [])

    def test_T1_real_dockerfile(self):
        self.assertEqual(kinds(read_real(), *self.KINDS), [])


class T2StagesTests(unittest.TestCase):
    KINDS = ("stages", "copy", "add")

    def test_T2_compliant_has_no_finding(self):
        self.assertEqual(kinds(GOOD, *self.KINDS), [])

    def test_T2_single_stage_is_reported(self):
        single = f"FROM nginx@sha256:{D2}\nUSER 101:101\n"
        self.assertTrue(kinds(single, "stages"))

    def test_T2_three_stages_are_reported(self):
        three = GOOD + f"\nFROM nginx@sha256:{D2}\nUSER 101:101\n"
        self.assertTrue(kinds(three, "stages"))

    def test_T2_copy_without_from_in_final_stage_is_reported(self):
        self.assertTrue(kinds(with_final("COPY site /x"), "copy"))

    def test_T2_copy_from_unknown_stage_is_reported(self):
        self.assertTrue(kinds(with_final("COPY --from=other /a /b"), "copy"))

    def test_T2_add_in_final_stage_is_reported(self):
        self.assertTrue(kinds(with_final("ADD a.tar /x"), "add"))

    def test_T2_add_in_build_stage_is_allowed(self):
        text = GOOD.replace("WORKDIR /src", "WORKDIR /src\nADD a.tar /x")
        self.assertEqual(kinds(text, "add"), [])

    def test_T2_real_dockerfile(self):
        self.assertEqual(kinds(read_real(), *self.KINDS), [])


class T3FinalStageTests(unittest.TestCase):
    KINDS = ("user", "install", "env")

    def test_T3_compliant_has_no_finding(self):
        self.assertEqual(kinds(GOOD, *self.KINDS), [])

    def test_T3_bad_users_are_reported(self):
        for bad in ("USER root", "USER 0", "USER 0:0", "USER nginx", "USER 101:0"):
            with self.subTest(user=bad):
                self.assertTrue(kinds(GOOD.replace("USER 101:101", bad), "user"))

    def test_T3_missing_user_is_reported(self):
        self.assertTrue(kinds(GOOD.replace("USER 101:101\n", ""), "user"))

    def test_T3_package_tools_in_final_stage_are_reported(self):
        for cmd in ("apk add curl", "apt install x", "apt-get update", "curl -s x", "wget x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(kinds(with_final("RUN " + cmd), "install"))

    def test_T3_package_tools_in_build_stage_are_allowed(self):
        text = GOOD.replace("WORKDIR /src", "WORKDIR /src\nRUN apt-get update")
        self.assertEqual(kinds(text, "install"), [])

    def test_T3_env_or_arg_with_host_ip_or_path_is_reported(self):
        for line in ("ENV H=10.1.2.3", "ENV H=dev.azure.com", "ARG X=192.168.0.5", "ENV P=/home/me",
                     "ENV P=/Users/me", "ARG P=C:\\work", "ENV P=/etc/secret"):
            with self.subTest(line=line):
                self.assertTrue(kinds(with_final(line), "env"))

    def test_T3_harmless_env_is_allowed(self):
        self.assertEqual(kinds(with_final("ENV NGINX_PORT=8080"), "env"), [])

    def test_T3_real_dockerfile(self):
        self.assertEqual(kinds(read_real(), *self.KINDS), [])


class T4NegativeFromTests(unittest.TestCase):
    def test_T4_each_bad_from_is_reported(self):
        bad = {
            "tag": "python:3.12",
            "latest": "python:latest",
            "tag with digest": f"python:3.12@sha256:{D1}",
            "no digest": "python",
            "short digest": "python@sha256:abc",
            "latest with digest": f"python:latest@sha256:{D1}",
        }
        for name, ref in bad.items():
            with self.subTest(case=name):
                self.assertTrue(kinds(GOOD.replace(f"python@sha256:{D1}", ref), "from"))

    def test_T4_every_bad_from_gets_its_own_finding(self):
        text = GOOD.replace(f"python@sha256:{D1}", "python:3.12").replace(f"nginx@sha256:{D2}", "nginx:latest")
        self.assertEqual(len(kinds(text, "from")), 2)


class T5LicenseTests(unittest.TestCase):
    def test_T5_compliant_has_no_license_finding(self):
        self.assertEqual(kinds(GOOD, "license"), [])

    def test_T5_missing_license_copy_is_reported(self):
        text = GOOD.replace("COPY --from=build /src/LICENSE /usr/share/nginx/html/LICENSE\n", "")
        self.assertTrue(kinds(text, "license"))

    def test_T5_license_copied_in_build_stage_only_is_reported(self):
        text = GOOD.replace("COPY --from=build /src/LICENSE /usr/share/nginx/html/LICENSE\n", "")
        text = text.replace("WORKDIR /src", "WORKDIR /src\nCOPY LICENSE /src/LICENSE")
        self.assertTrue(kinds(text, "license"))

    def test_T5_real_dockerfile(self):
        self.assertEqual(kinds(read_real(), "license"), [])


class T6UnsupportedTests(unittest.TestCase):
    def test_T6_compliant_and_real_have_no_unsupported_finding(self):
        self.assertEqual(kinds(GOOD, "unsupported"), [])
        self.assertEqual(kinds(read_real(), "unsupported"), [])

    def test_T6_plain_comment_and_arg_before_first_from_are_allowed(self):
        text = "# python:3.12 pinned below\nARG VERSION=1\n" + GOOD
        self.assertEqual(kinds(text, "unsupported"), [])

    def test_T6_heredoc_is_reported(self):
        text = GOOD.replace("RUN python scripts/build_site.py --out /out",
                            "RUN <<EOF\npython scripts/build_site.py --out /out\nEOF")
        self.assertTrue(kinds(text, "unsupported"))

    def test_T6_heredoc_in_final_stage_is_reported(self):
        self.assertTrue(kinds(with_final("RUN <<EOF\napk add curl\nEOF"), "unsupported"))

    def test_T6_parser_directives_are_reported(self):
        for line in ("# escape=`", "# syntax=docker/dockerfile:1"):
            with self.subTest(directive=line):
                self.assertTrue(kinds(line + "\n" + GOOD, "unsupported"))

    def test_T6_instruction_before_first_from_other_than_arg_is_reported(self):
        for line in ("RUN echo hi", "ENV A=1", "COPY a b"):
            with self.subTest(line=line):
                self.assertTrue(kinds(line + "\n" + GOOD, "unsupported"))

    def test_T6_arg_before_first_from_with_private_value_is_an_env_finding(self):
        for line in ("ARG B=10.1.2.3", "ARG B=/home/me", "ARG B=dev.azure.com"):
            with self.subTest(line=line):
                self.assertTrue(kinds(line + "\n" + GOOD, "env"))

    def test_T6_onbuild_is_reported(self):
        self.assertTrue(kinds(with_final("ONBUILD RUN echo hi"), "unsupported"))
        self.assertTrue(kinds(GOOD.replace("WORKDIR /src", "WORKDIR /src\nONBUILD RUN echo hi"), "unsupported"))


class T7LabelTests(unittest.TestCase):
    def test_T7_label_with_host_ip_or_path_is_an_env_finding(self):
        for value in ("10.1.2.3", "dev.azure.com", "/home/me", "/etc/x", r"C:\work"):
            with self.subTest(value=value):
                self.assertTrue(kinds(with_final(f'LABEL org.example.src="{value}"'), "env"))

    def test_T7_harmless_label_is_clean(self):
        text = with_final('LABEL org.opencontainers.image.title="venom site"')
        self.assertEqual(kinds(text, "env", "unsupported"), [])


class T8WholeContextCopyTests(unittest.TestCase):
    def build_with(self, line: str) -> str:
        return GOOD.replace("WORKDIR /src", "WORKDIR /src\n" + line)

    def test_T8_whole_context_copy_is_reported_in_build_stage(self):
        for line in ("COPY . .", "COPY ./ /src/", "COPY * /src/", "COPY --chown=1:1 . /src",
                     "COPY ./* /src/", "COPY .* /src/", "COPY / /src/", "ADD . /src", "COPY --chmod=755 * ./"):
            with self.subTest(line=line):
                self.assertTrue(kinds(self.build_with(line), "copy"))

    def test_T8_whole_context_copy_is_reported_in_final_stage(self):
        self.assertTrue(kinds(with_final("COPY . /x"), "copy"))

    def test_T8_named_paths_are_clean(self):
        for line in ("COPY site/ site/", "COPY README.md LICENSE ./", "COPY executors/ executors/",
                     "COPY CONTRIBUTING.md ./", "COPY --from=build /out/ /dest/"):
            with self.subTest(line=line):
                self.assertEqual(kinds(self.build_with(line.replace("--from=build ", "")), "copy"), [])

    def test_T8_copy_from_build_stage_is_clean(self):
        self.assertEqual(kinds(with_final("COPY --from=build /out/ /dest/"), "copy"), [])

    def test_T8_compliant_fixture_is_clean(self):
        self.assertEqual(kinds(GOOD, "copy"), [])

    def test_T8_real_dockerfile_has_no_whole_context_copy(self):
        text = read_real()
        whole = [ln for ln in text.splitlines()
                 if re.match(r"\s*(COPY|ADD)\s+(--\S+\s+)*(\.|\./|/|\*|\./\*|\.\*)\s", ln, re.I)]
        self.assertEqual(whole, [])
        self.assertEqual(kinds(text, "copy"), [])


if __name__ == "__main__":
    unittest.main()
