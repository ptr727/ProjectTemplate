#!/usr/bin/env python3
"""Exercise scripts/agent_policy.py and the places that consume its render.

The renderer decides which bots review a repository and whether its rulesets carry the Copilot rule.
So each case pins one decision against a constructed registry and a throwaway checkout.
The configure.sh case runs the script's own composition lines rather than a restatement of them.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from test_configure_archived import lift, run_bash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "spec"))

import agent_policy
import validate

COMPOSE = lift(r"(compose_ruleset\(\) \{.*?\n\}\n)")


def registry(**override: object) -> dict[str, object]:
    entry: dict[str, object] = {"name": "Fixture"}
    if override:
        entry["agentPolicy"] = {"review": {"bots": override}}
    return {"repos": [entry]}


def git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8")


def make_checkout(tmp: str, origin: str = "https://github.com/Acme/Widget.git") -> Path:
    path = Path(tmp) / "checkout"
    path.mkdir()
    git("-C", str(path), "init", "-q")
    git("-C", str(path), "remote", "add", "origin", origin)
    return path


class RenderCase(unittest.TestCase):
    def test_default_render_keeps_key_order_and_empty_guard(self) -> None:
        rendered = agent_policy.render(registry(), "Fixture", "release")
        self.assertEqual(list(rendered), ["$schema", "version", "review", "guard"])
        self.assertEqual(rendered["guard"], {})
        self.assertTrue(str(rendered["$schema"]).startswith("https://raw.githubusercontent.com/"))
        text = agent_policy.serialize(rendered)
        self.assertTrue(text.endswith("}\n"))
        self.assertNotIn("\r", text)

    def test_absent_name_and_absent_registry_render_the_default(self) -> None:
        base = agent_policy.render(None, "Fixture", "release")
        self.assertEqual(agent_policy.render(registry(), "Other", "release"), base)

    def test_operational_adds_the_commit_in_place_guard(self) -> None:
        guard = agent_policy.render(None, "Fixture", "operational")["guard"]
        self.assertEqual(guard, {"primaryCheckout": {"commitInPlace": {"branches": ["develop"]}}})

    def test_override_merges_shallowly_per_bot(self) -> None:
        rendered = agent_policy.render(
            registry(copilot={"trigger": "explicit", "when": "after-checks"}), "Fixture", "release"
        )
        bots = rendered["review"]["bots"]
        self.assertEqual(bots["copilot"]["trigger"], "explicit")
        self.assertEqual(bots["copilot"]["when"], "after-checks")
        self.assertEqual(bots["copilot"]["bases"], ["@default"])
        self.assertEqual(bots["qodo"]["trigger"], "auto")

    def test_malformed_overrides_are_refused(self) -> None:
        bad: list[dict[str, object]] = [
            {"agentPolicy": None},
            {"agentPolicy": {"guard": {}}},
            {"agentPolicy": {"review": {"bots": {"nobot": {}}}}},
            {"agentPolicy": {"review": {"bots": {"qodo": "off"}}}},
            {"agentPolicy": {"review": {"bots": {"qodo": {"trigger": "sometimes"}}}}},
            {"agentPolicy": {"review": {"bots": {"qodo": {"stray": 1}}}}},
        ]
        for extra in bad:
            with self.subTest(extra=extra):
                data = {"repos": [{"name": "Fixture", **extra}]}
                with self.assertRaises(agent_policy.Refusal) as ctx:
                    agent_policy.render(data, "Fixture", "release")
                self.assertIn("Fixture", str(ctx.exception))

    def test_auto_coderabbit_without_the_default_branch_is_refused(self) -> None:
        with self.assertRaises(agent_policy.Refusal) as ctx:
            agent_policy.render(registry(coderabbit={"bases": ["^develop$"]}), "Fixture", "release")
        self.assertIn("@default", str(ctx.exception))
        off = registry(coderabbit={"trigger": "off", "bases": ["^develop$"]})
        agent_policy.render(off, "Fixture", "release")


class FactsCase(unittest.TestCase):
    def facts(self, **override: object) -> dict[str, object]:
        return agent_policy.facts(agent_policy.render(registry(**override), "Fixture", "release"))

    def test_default_facts(self) -> None:
        self.assertEqual(
            self.facts(),
            {
                "copilotRule": {"main": True, "develop": False},
                "coderabbit": {"enabled": True, "base_branches": ["^develop$"]},
                "qodo": {"disable_auto_feedback": False},
            },
        )

    def test_explicit_copilot_gives_no_rule(self) -> None:
        got = self.facts(copilot={"trigger": "explicit"})
        self.assertEqual(got["copilotRule"], {"main": False, "develop": False})

    def test_auto_copilot_covering_develop_gives_both_rules(self) -> None:
        got = self.facts(copilot={"bases": ["@default", "^develop$"]})
        self.assertEqual(got["copilotRule"], {"main": True, "develop": True})

    def test_non_auto_bots_map_to_their_settings(self) -> None:
        got = self.facts(
            coderabbit={"trigger": "explicit", "bases": ["^release$"]}, qodo={"trigger": "off"}
        )
        self.assertEqual(got["coderabbit"], {"enabled": False, "base_branches": ["^release$"]})
        self.assertEqual(got["qodo"], {"disable_auto_feedback": True})


class WriteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = make_checkout(self.tmp.name)
        self.rendered = agent_policy.render(None, "Fixture", "release")

    def test_absent_files_are_created_and_a_second_run_writes_nothing(self) -> None:
        first = agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertEqual([line.split()[0] for line in first], ["wrote"] * 3)
        second = agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertEqual([line.split()[0] for line in second], ["unchanged"] * 3)
        policy = self.checkout / ".github" / "agent-policy.json"
        self.assertEqual(policy.read_text(encoding="utf-8"), agent_policy.serialize(self.rendered))

    def test_yaml_and_toml_keep_everything_else(self) -> None:
        (self.checkout / ".coderabbit.yaml").write_text(
            "# lead\nreviews:\n  profile: chill  # keep\n  auto_review:\n    drafts: false\nlanguage: en-US\n",
            encoding="utf-8",
        )
        (self.checkout / ".pr_agent.toml").write_text(
            "[pr_reviewer]\nrequire_tests = true # keep\n\n[config]\nmodel = 'x'\n",
            encoding="utf-8",
        )
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        yaml_text = (self.checkout / ".coderabbit.yaml").read_text(encoding="utf-8")
        for kept in ("# lead", "profile: chill  # keep", "drafts: false", "language: en-US"):
            self.assertIn(kept, yaml_text)
        self.assertIn("enabled: true", yaml_text)
        self.assertIn("^develop$", yaml_text)
        toml_text = (self.checkout / ".pr_agent.toml").read_text(encoding="utf-8")
        for kept in ("[pr_reviewer]", "require_tests = true # keep", "model = 'x'"):
            self.assertIn(kept, toml_text)
        self.assertIn("disable_auto_feedback = false", toml_text)

    def test_a_crlf_file_stays_crlf_and_an_lf_file_stays_lf(self) -> None:
        crlf = {
            ".coderabbit.yaml": "# lead\r\nreviews:\r\n  profile: chill\r\n",
            ".pr_agent.toml": "[pr_reviewer]\r\nrequire_tests = true\r\n",
            ".github/agent-policy.json": "{}\r\n",
        }
        for name, text in crlf.items():
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        for name in crlf:
            with self.subTest(name=name):
                out = (self.checkout / name).read_bytes()
                self.assertIn(b"\r\n", out)
                self.assertNotIn(b"\n", out.replace(b"\r\n", b""))
        second = agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertEqual([line.split()[0] for line in second], ["unchanged"] * 3)
        with tempfile.TemporaryDirectory() as tmp:
            lf = make_checkout(tmp)
            agent_policy.write(self.rendered, lf, "acme/widget")
            for name in crlf:
                with self.subTest(lf=name):
                    self.assertNotIn(b"\r", (lf / name).read_bytes())

    def test_a_checkout_of_another_repo_is_refused_before_any_write(self) -> None:
        with self.assertRaises(agent_policy.Refusal):
            agent_policy.write(self.rendered, self.checkout, "acme/other")
        self.assertFalse((self.checkout / ".github").exists())

    def test_ssh_and_https_origins_compare_case_insensitively(self) -> None:
        for url in ("git@github.com:ACME/widget.git", "https://github.com/acme/WIDGET"):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                path = make_checkout(tmp, url)
                agent_policy.verify_checkout(path, "Acme/Widget")

    def test_a_directory_that_is_not_a_work_tree_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(agent_policy.Refusal):
            agent_policy.verify_checkout(Path(tmp), "acme/widget")


class PlanCase(unittest.TestCase):
    """The checkout is planned whole before any file is written."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = make_checkout(self.tmp.name)
        self.rendered = agent_policy.render(None, "Fixture", "release")

    def test_dry_run_reports_and_writes_nothing(self) -> None:
        lines = agent_policy.write(self.rendered, self.checkout, "acme/widget", dry_run=True)
        self.assertEqual([line.split()[:2] for line in lines], [["would", "write"]] * 3)
        self.assertFalse((self.checkout / ".github").exists())
        self.assertFalse((self.checkout / ".coderabbit.yaml").exists())
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        again = agent_policy.write(self.rendered, self.checkout, "acme/widget", dry_run=True)
        self.assertEqual([line.split()[0] for line in again], ["unchanged"] * 3)

    def test_a_malformed_file_leaves_the_policy_file_unwritten(self) -> None:
        (self.checkout / ".pr_agent.toml").write_text("[config\nbroken", encoding="utf-8")
        with self.assertRaises(agent_policy.Refusal):
            agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertFalse((self.checkout / ".github" / "agent-policy.json").exists())
        self.assertFalse((self.checkout / ".coderabbit.yaml").exists())

    def test_a_malformed_yaml_file_is_refused_by_the_dry_run_too(self) -> None:
        (self.checkout / ".coderabbit.yaml").write_text("reviews: [\n", encoding="utf-8")
        with self.assertRaises(agent_policy.Refusal):
            agent_policy.write(self.rendered, self.checkout, "acme/widget", dry_run=True)


HUB_SHAPED = """# Lead comment.
reviews:
  # Explains the list.
  auto_review:
    enabled: true  # trailing note
    base_branches:
      - "^develop$"
    # Comment after the list.
    # Second comment line.
    auto_pause_after_reviewed_commits: 0
  path_filters:
    - "!docs/**"
"""


class YamlPreservationCase(unittest.TestCase):
    """Changing the CodeRabbit keys keeps every comment and the quoting of the list."""

    def apply(self, enabled: bool, branches: list[str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(HUB_SHAPED, encoding="utf-8")
            text = agent_policy.plan_yaml(path, {"enabled": enabled, "base_branches": branches})
        self.assertIsNotNone(text)
        assert text is not None
        return text

    def lines(self, text: str) -> list[str]:
        return text.splitlines()

    def test_an_unchanged_file_plans_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(HUB_SHAPED, encoding="utf-8")
            want = {"enabled": True, "base_branches": ["^develop$"]}
            self.assertIsNone(agent_policy.plan_yaml(path, want))

    def test_a_grown_list_keeps_the_comment_after_it(self) -> None:
        got = self.apply(True, ["^develop$", "^release/.*$"])
        expected = HUB_SHAPED.replace(
            '      - "^develop$"\n', '      - "^develop$"\n      - "^release/.*$"\n'
        )
        self.assertEqual(got, expected)

    def test_a_shrunk_list_keeps_the_comment_and_the_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(
                HUB_SHAPED.replace(
                    '      - "^develop$"\n', '      - "^develop$"\n      - "^old$"\n'
                ),
                encoding="utf-8",
            )
            got = agent_policy.plan_yaml(path, {"enabled": True, "base_branches": ["^develop$"]})
        self.assertEqual(got, HUB_SHAPED)

    def test_an_emptied_list_keeps_both_comment_lines(self) -> None:
        got = self.apply(True, [])
        self.assertEqual(agent_policy_yaml_load(got)["reviews"]["auto_review"]["base_branches"], [])
        self.assertIn("# Comment after the list.", got)
        self.assertIn("    # Second comment line.", got)
        lines = self.lines(got)
        self.assertIn("    base_branches: []", lines)
        at = lines.index("    # Comment after the list.")
        self.assertEqual(lines[at + 1], "    # Second comment line.")
        self.assertEqual(lines[at + 2], "    auto_pause_after_reviewed_commits: 0")
        self.assertEqual(lines[at - 1], "    base_branches: []")
        original = self.lines(HUB_SHAPED)
        original.remove('      - "^develop$"')
        original[original.index("    base_branches:")] = "    base_branches: []"
        self.assertEqual(lines, original)

    def test_an_enabled_flip_keeps_the_trailing_comment(self) -> None:
        got = self.apply(False, ["^develop$"])
        self.assertEqual(got, HUB_SHAPED.replace("enabled: true  #", "enabled: false #"))


def agent_policy_yaml_load(text: str) -> dict[str, dict[str, dict[str, object]]]:
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(text)


class CheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rendered = agent_policy.render(None, "Fixture", "release")
        self.files = {
            ".github/agent-policy.json": agent_policy.serialize(self.rendered),
            ".coderabbit.yaml": "reviews:\n  auto_review:\n    enabled: true\n    base_branches:\n      - ^develop$\n",
            ".pr_agent.toml": "[config]\ndisable_auto_feedback = false\n",
        }

    def run_check(self, files: Mapping[str, str | None | Exception]) -> list[agent_policy.Result]:
        def read(path: str) -> str | None:
            got = files[path]
            if isinstance(got, Exception):
                raise got
            return got

        return agent_policy.check(self.rendered, read)

    def failures(self, files: Mapping[str, str | None | Exception]) -> list[str]:
        return [r.msg for r in self.run_check(files) if not r.ok]

    def test_matching_files_pass(self) -> None:
        self.assertEqual(self.failures(dict(self.files)), [])

    def test_drift_in_each_file_fails(self) -> None:
        drifted = copy.deepcopy(self.files)
        drifted[".github/agent-policy.json"] = "{}"
        drifted[".coderabbit.yaml"] = drifted[".coderabbit.yaml"].replace("^develop$", "^dev$")
        drifted[".pr_agent.toml"] = "[config]\ndisable_auto_feedback = true\n"
        self.assertEqual(len(self.failures(drifted)), 3)

    def test_base_branches_compare_as_a_set(self) -> None:
        rendered = agent_policy.render(
            registry(coderabbit={"bases": ["@default", "^a$", "^b$"]}), "Fixture", "release"
        )
        files = dict(self.files)
        files[".github/agent-policy.json"] = agent_policy.serialize(rendered)
        files[".coderabbit.yaml"] = (
            "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^b$', '^a$']\n"
        )
        got = agent_policy.check(rendered, lambda path: files[path])
        self.assertTrue(all(r.ok for r in got), got)

    def test_an_absent_qodo_key_fails(self) -> None:
        files = dict(self.files)
        files[".pr_agent.toml"] = "[config]\nmodel = 'x'\n"
        self.assertEqual(len(self.failures(files)), 1)

    def test_a_missing_file_fails_naming_the_fix(self) -> None:
        files: dict[str, str | None | Exception] = dict(self.files)
        files[".coderabbit.yaml"] = None
        (msg,) = self.failures(files)
        self.assertIn("missing", msg)
        self.assertIn("configure.sh apply --checkout", msg)

    def test_an_unreadable_file_says_so(self) -> None:
        files: dict[str, str | None | Exception] = dict(self.files)
        files[".pr_agent.toml"] = agent_policy.Unreadable("HTTP 500")
        (msg,) = self.failures(files)
        self.assertIn("unreadable", msg)
        self.assertNotIn("configure.sh apply", msg)


class GhReaderCase(unittest.TestCase):
    """The contents reader, run against a stand-in runner that separates absent from broken."""

    def reader(self, code: int, out: str, err: str) -> agent_policy.Reader:
        def run(argv: list[str]) -> tuple[int, str, str]:
            self.assertEqual(argv[:2], ["gh", "api"])
            return code, out, err

        return agent_policy.gh_reader("acme/widget", "main", run)

    def test_content_absent_and_broken_reads(self) -> None:
        content = base64.encodebytes(b"hello\n").decode()
        ok = self.reader(0, json.dumps(content), "")
        self.assertEqual(ok(".pr_agent.toml"), "hello\n")
        gone = self.reader(1, "", "gh: Not Found (HTTP 404)")
        self.assertIsNone(gone(".pr_agent.toml"))
        broken = self.reader(1, "", "gh: Server Error (HTTP 500)")
        with self.assertRaises(agent_policy.Unreadable):
            broken(".pr_agent.toml")


class RefusalCase(unittest.TestCase):
    def test_an_auto_qodo_must_cover_main_and_develop(self) -> None:
        for bases in (["@default"], ["^develop$"]):
            with self.subTest(bases=bases), self.assertRaises(agent_policy.Refusal) as ctx:
                agent_policy.render(registry(qodo={"bases": bases}), "Fixture", "release")
            self.assertIn("qodo", str(ctx.exception))
            self.assertIn("every pull request", str(ctx.exception))
        agent_policy.render(registry(qodo={"bases": ["^(main|develop)$"]}), "Fixture", "release")
        agent_policy.render(
            registry(qodo={"trigger": "explicit", "bases": ["@default"]}), "Fixture", "release"
        )


class AnchorCase(unittest.TestCase):
    def test_anchored_bases_stay_and_others_are_wrapped(self) -> None:
        self.assertEqual(agent_policy.anchored("^develop$"), "^develop$")
        self.assertEqual(agent_policy.anchored("release/.*"), "^(?:release/.*)$")
        self.assertEqual(agent_policy.anchored("^main|develop$"), "^(?:^main|develop$)$")
        self.assertEqual(agent_policy.anchored("^a|b"), "^(?:^a|b)$")
        self.assertEqual(agent_policy.anchored("^a\\$"), "^(?:^a\\$)$")
        self.assertEqual(agent_policy.anchored("^a\\\\$"), "^a\\\\$")

    def test_facts_and_check_use_the_anchored_form(self) -> None:
        rendered = agent_policy.render(
            registry(coderabbit={"bases": ["@default", "^develop$", "release/.*"]}),
            "Fixture",
            "release",
        )
        want = agent_policy.facts(rendered)["coderabbit"]["base_branches"]
        self.assertEqual(want, ["^develop$", "^(?:release/.*)$"])
        files = {
            ".github/agent-policy.json": agent_policy.serialize(rendered),
            ".coderabbit.yaml": "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^develop$', 'release/.*']\n",
            ".pr_agent.toml": "[config]\ndisable_auto_feedback = false\n",
        }
        got = agent_policy.check(rendered, lambda path: files[path])
        self.assertEqual(next(r.msg for r in got if not r.ok).split()[0], ".coderabbit.yaml")


class TopLevelCase(unittest.TestCase):
    def test_a_subdirectory_or_a_symlink_to_the_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            top = make_checkout(tmp)
            (top / "sub").mkdir()
            with self.assertRaises(agent_policy.Refusal) as ctx:
                agent_policy.verify_checkout(top / "sub", "acme/widget")
            self.assertIn("top level", str(ctx.exception))
            link = Path(tmp) / "link"
            link.symlink_to(top, target_is_directory=True)
            agent_policy.verify_checkout(link, "acme/widget")


class FactsImportCase(unittest.TestCase):
    def test_the_facts_path_imports_nothing_third_party(self) -> None:
        code = (
            "import sys, runpy;"
            f"sys.argv=['agent_policy.py','facts','--registry','none.json','--name','X','--model','release'];"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r});"
            "import agent_policy, pr_review_policy;"
            "agent_policy.main(sys.argv[1:]);"
            "bad=[m for m in ('ruamel','tomlkit','markdown_it','yaml') if m in sys.modules];"
            "sys.exit(1 if bad else 0)"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)


class CheckExitCase(unittest.TestCase):
    def run_main(self, files: dict[str, str | None | Exception]) -> tuple[int, str]:
        def read(path: str) -> str | None:
            got = files[path]
            if isinstance(got, Exception):
                raise got
            return got

        out = io.StringIO()
        argv = [
            "check", "--registry", "none.json", "--name", "X", "--model", "release",
            "--repo", "acme/widget", "--ref", "main",
        ]  # fmt: skip
        with (
            mock.patch.object(agent_policy, "gh_reader", lambda repo, ref: read),
            contextlib.redirect_stdout(out),
        ):
            return agent_policy.main(argv), out.getvalue()

    def good(self) -> dict[str, str | None | Exception]:
        rendered = agent_policy.render(None, "X", "release")
        return {
            ".github/agent-policy.json": agent_policy.serialize(rendered),
            ".coderabbit.yaml": "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^develop$']\n",
            ".pr_agent.toml": "[config]\ndisable_auto_feedback = false\n",
        }

    def test_exit_codes(self) -> None:
        self.assertEqual(self.run_main(self.good())[0], 0)
        missing = self.good()
        missing[".pr_agent.toml"] = None
        self.assertEqual(self.run_main(missing)[0], 1)
        broken = self.good()
        broken[".coderabbit.yaml"] = agent_policy.Unreadable("HTTP 500")
        broken[".pr_agent.toml"] = None
        code, out = self.run_main(broken)
        self.assertEqual(code, 2)
        self.assertIn("unreadable", out)
        self.assertIn(".pr_agent.toml is missing", out)
        self.assertIn("ok", out)


class CommandLineCase(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent_policy.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )

    def test_help_exits_zero_and_an_unknown_argument_is_refused(self) -> None:
        self.assertEqual(self.run_cli("--help").returncode, 0)
        self.assertEqual(self.run_cli("render", "--help").returncode, 0)
        refused = self.run_cli(
            "render", "--registry", "r", "--name", "n", "--model", "release", "--bogus"
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("unrecognized arguments", refused.stderr)

    def test_render_prints_the_file_for_a_missing_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = ["render", "--registry", str(Path(tmp) / "none.json")]
            done = self.run_cli(*args, "--name", "Fixture", "--model", "release")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["version"], 1)

    def test_a_refusal_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.json"
            data = registry(coderabbit={"bases": ["^develop$"]})
            path.write_text(json.dumps(data), encoding="utf-8")
            done = self.run_cli(
                "facts", "--registry", str(path), "--name", "Fixture", "--model", "release"
            )
        self.assertEqual(done.returncode, 1)


class ComposeRulesetCase(unittest.TestCase):
    """configure.sh adds or strips the Copilot rule on a composed payload by the derived fact."""

    def compose(self, src: Path, want: str) -> list[str]:
        if shutil.which("jq") is None:
            raise unittest.SkipTest("no jq on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            main = ROOT / "repo-config" / "main.json"
            quoted = [shlex.quote(str(p)) for p in (main, src, out)]
            script = (
                f"main_source={quoted[0]}\n{COMPOSE}"
                f"compose_ruleset {quoted[1]} {want} {quoted[2]}\n"
            )
            done = run_bash(script, "jq")
            self.assertEqual(done.returncode, 0, done.stderr)
            return [rule["type"] for rule in json.loads(out.read_text(encoding="utf-8"))["rules"]]

    def test_main_keeps_or_loses_the_rule(self) -> None:
        main = ROOT / "repo-config" / "main.json"
        self.assertEqual(self.compose(main, "true").count("copilot_code_review"), 1)
        self.assertNotIn("copilot_code_review", self.compose(main, "false"))

    def test_develop_gains_the_rule_only_when_wanted(self) -> None:
        for name in ("develop.json", "operational/develop.json"):
            with self.subTest(payload=name):
                src = ROOT / "repo-config" / name
                self.assertNotIn("copilot_code_review", self.compose(src, "false"))
                self.assertEqual(self.compose(src, "true").count("copilot_code_review"), 1)


class ValidateCase(unittest.TestCase):
    def test_a_malformed_agent_policy_is_reported_and_a_good_one_is_not(self) -> None:
        bad = {"name": "Fixture", "agentPolicy": {"review": {"bots": {"qodo": {"trigger": "x"}}}}}
        errors = validate.agent_policy_errors_for_repo(bad, "Fixture", "release")
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("Fixture: agentPolicy"), errors)
        null = {"name": "Fixture", "agentPolicy": None}
        self.assertEqual(len(validate.agent_policy_errors_for_repo(null, "Fixture", "release")), 1)
        good = {
            "name": "Fixture",
            "agentPolicy": {"review": {"bots": {"qodo": {"trigger": "off"}}}},
        }
        self.assertEqual(validate.agent_policy_errors_for_repo(good, "Fixture", "release"), [])
        self.assertEqual(
            validate.agent_policy_errors_for_repo({"name": "Fixture"}, "Fixture", "release"), []
        )


if __name__ == "__main__":
    unittest.main()
