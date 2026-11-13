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
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "spec"))

import agent_policy
import validate


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
    (path / ".editorconfig").write_text("root = true\n", encoding="utf-8")
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
                "coderabbit": {
                    "enabled": True,
                    "base_branches": ["^develop$"],
                    "auto_pause_after_reviewed_commits": 0,
                    "path_filters": ["!.github/skills/**", "!lib/fleet/sh/**"],
                },
                "qodo": {
                    "disable_auto_feedback": False,
                    "ignore_glob": [".github/skills/**", "lib/fleet/sh/**"],
                },
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
        self.assertEqual(
            got["coderabbit"],
            {
                "enabled": False,
                "base_branches": ["^release$"],
                "auto_pause_after_reviewed_commits": 0,
                "path_filters": ["!.github/skills/**", "!lib/fleet/sh/**"],
            },
        )
        self.assertEqual(
            got["qodo"],
            {
                "disable_auto_feedback": True,
                "ignore_glob": [".github/skills/**", "lib/fleet/sh/**"],
            },
        )


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

    def test_a_new_file_takes_its_endings_from_the_editorconfig(self) -> None:
        (self.checkout / ".editorconfig").write_text(
            "root = true\n\n[*]\nend_of_line = lf\n\n[*.{json,yaml}]\nend_of_line = crlf\n",
            encoding="utf-8",
        )
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        for name in (".coderabbit.yaml", ".github/agent-policy.json"):
            with self.subTest(crlf=name):
                out = (self.checkout / name).read_bytes()
                self.assertIn(b"\r\n", out)
                self.assertNotIn(b"\n", out.replace(b"\r\n", b""))
        self.assertNotIn(b"\r", (self.checkout / ".pr_agent.toml").read_bytes())
        second = agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertEqual([line.split()[0] for line in second], ["unchanged"] * 3)

    def test_an_existing_file_keeps_its_endings_over_the_editorconfig(self) -> None:
        (self.checkout / ".editorconfig").write_text(
            "root = true\n\n[*]\nend_of_line = crlf\n", encoding="utf-8"
        )
        (self.checkout / ".pr_agent.toml").write_text(
            "[pr_reviewer]\n", encoding="utf-8", newline=""
        )
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        self.assertNotIn(b"\r", (self.checkout / ".pr_agent.toml").read_bytes())
        self.assertIn(b"\r\n", (self.checkout / ".coderabbit.yaml").read_bytes())

    def test_an_integer_qodo_setting_is_rewritten(self) -> None:
        (self.checkout / ".pr_agent.toml").write_text(
            "[config]\ndisable_auto_feedback = 0\n", encoding="utf-8"
        )
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        text = (self.checkout / ".pr_agent.toml").read_text(encoding="utf-8")
        self.assertIn("disable_auto_feedback = false", text)

    def test_a_boolean_pause_and_an_integer_enabled_are_rewritten(self) -> None:
        (self.checkout / ".coderabbit.yaml").write_text(
            'reviews:\n  auto_review:\n    enabled: 1\n    base_branches:\n      - "^develop$"\n'
            "    auto_pause_after_reviewed_commits: false\n",
            encoding="utf-8",
        )
        (line,) = [
            x
            for x in agent_policy.write(self.rendered, self.checkout, "acme/widget")
            if "yaml" in x
        ]
        self.assertTrue(line.startswith("wrote"), line)
        doc = agent_policy_yaml_load(
            (self.checkout / ".coderabbit.yaml").read_text(encoding="utf-8")
        )
        auto = doc["reviews"]["auto_review"]
        self.assertIs(auto["enabled"], True)
        self.assertEqual(type(auto["auto_pause_after_reviewed_commits"]), int)

    def test_the_pause_is_added_beside_the_keys_already_there(self) -> None:
        (self.checkout / ".coderabbit.yaml").write_text(
            'reviews:\n  auto_review:\n    enabled: true\n    base_branches:\n      - "^develop$"\n    drafts: false\n',
            encoding="utf-8",
        )
        agent_policy.write(self.rendered, self.checkout, "acme/widget")
        doc = agent_policy_yaml_load(
            (self.checkout / ".coderabbit.yaml").read_text(encoding="utf-8")
        )
        auto = doc["reviews"]["auto_review"]
        self.assertEqual(auto["auto_pause_after_reviewed_commits"], 0)
        self.assertEqual(auto["drafts"], False)

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


def want(enabled: bool, branches: list[str], filters: list[str] | None = None) -> dict[str, object]:
    return {
        "enabled": enabled,
        "base_branches": branches,
        "auto_pause_after_reviewed_commits": 0,
        "path_filters": ["!docs/**"] if filters is None else filters,
    }


class YamlPreservationCase(unittest.TestCase):
    """Changing the CodeRabbit keys keeps every comment and the quoting of the list."""

    def apply(self, enabled: bool, branches: list[str]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(HUB_SHAPED, encoding="utf-8")
            text = agent_policy.plan_yaml(path, want(enabled, branches))
        self.assertIsNotNone(text)
        assert text is not None
        return text

    def lines(self, text: str) -> list[str]:
        return text.splitlines()

    def test_an_unchanged_file_plans_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(HUB_SHAPED, encoding="utf-8")
            self.assertIsNone(agent_policy.plan_yaml(path, want(True, ["^develop$"])))

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
            got = agent_policy.plan_yaml(path, want(True, ["^develop$"]))
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


QODO_GOOD = "[ignore]\nglob = ['.github/skills/**', 'lib/fleet/sh/**']\n\n[config]\ndisable_auto_feedback = false\n"


class CarriedExclusionCase(unittest.TestCase):
    """Each rendered reviewer config skips the carried Skills tree, keeping the repository's own entries."""

    SKILLS = "!.github/skills/**"

    def plan_yaml(self, text: str) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".coderabbit.yaml"
            path.write_text(text, encoding="utf-8")
            return agent_policy.plan_yaml(path, want(True, ["^develop$"], [self.SKILLS]))

    def plan_toml(self, text: str | None) -> str | None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pr_agent.toml"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            wanted = {"disable_auto_feedback": False, "ignore_glob": [".github/skills/**"]}
            return agent_policy.plan_toml(path, wanted)

    def test_a_yaml_filter_is_appended_after_the_repository_s_own(self) -> None:
        got = self.plan_yaml(HUB_SHAPED)
        assert got is not None
        filters = agent_policy_yaml_load(got)["reviews"]["path_filters"]
        self.assertEqual(filters, ["!docs/**", self.SKILLS])

    def test_a_yaml_with_no_filters_gains_the_list(self) -> None:
        got = self.plan_yaml(
            "reviews:\n  auto_review:\n    enabled: true\n    base_branches:\n"
            '      - "^develop$"\n    auto_pause_after_reviewed_commits: 0\n'
        )
        assert got is not None
        self.assertEqual(agent_policy_yaml_load(got)["reviews"]["path_filters"], [self.SKILLS])

    def test_a_yaml_already_holding_the_filter_plans_nothing(self) -> None:
        text = HUB_SHAPED.replace(
            '    - "!docs/**"\n', f'    - "!docs/**"\n    - "{self.SKILLS}"\n'
        )
        self.assertIsNone(self.plan_yaml(text))

    def test_a_yaml_filter_that_is_not_a_list_is_refused(self) -> None:
        with self.assertRaises(agent_policy.Refusal):
            self.plan_yaml("reviews:\n  path_filters: '!docs/**'\n")

    def test_a_toml_glob_is_appended_after_the_repository_s_own(self) -> None:
        got = self.plan_toml(
            "[ignore]\nglob = ['docs/**']\n\n[config]\ndisable_auto_feedback = false\n"
        )
        assert got is not None
        self.assertEqual(tomllib.loads(got)["ignore"]["glob"], ["docs/**", ".github/skills/**"])

    def test_a_toml_with_no_ignore_table_gains_one(self) -> None:
        got = self.plan_toml("[config]\ndisable_auto_feedback = false\n")
        assert got is not None
        doc = tomllib.loads(got)
        self.assertEqual(doc["ignore"]["glob"], [".github/skills/**"])
        self.assertIs(doc["config"]["disable_auto_feedback"], False)

    def test_a_toml_already_holding_the_glob_plans_nothing(self) -> None:
        self.assertIsNone(self.plan_toml(QODO_GOOD))

    def test_a_toml_glob_that_is_not_a_list_is_refused(self) -> None:
        with self.assertRaises(agent_policy.Refusal):
            self.plan_toml("[ignore]\nglob = 'docs/**'\n")

    def test_a_comment_after_the_yaml_filters_stays_before_the_next_key(self) -> None:
        tail = "  # Explains the next key.\n  path_instructions: []\n"
        got = self.plan_yaml('reviews:\n  path_filters:\n    - "!docs/**"\n' + tail)
        assert got is not None
        self.assertIn(f'    - "!docs/**"\n    - "{self.SKILLS}"\n' + tail, got)

    def test_an_end_of_line_comment_stays_on_its_own_filter(self) -> None:
        tail = "  # Explains the next key.\n  path_instructions: []\n"
        got = self.plan_yaml('reviews:\n  path_filters:\n    - "!docs/**"  # generated\n' + tail)
        assert got is not None
        self.assertIn(f'    - "!docs/**"  # generated\n    - "{self.SKILLS}"\n' + tail, got)

    def test_a_toml_ignore_that_is_not_a_table_is_refused(self) -> None:
        with self.assertRaises(agent_policy.Refusal):
            self.plan_toml("ignore = 'docs/**'\n")

    def test_check_reports_a_malformed_ignore_as_drift(self) -> None:
        rendered = agent_policy.render(None, "Fixture", "release")
        want = agent_policy.facts(rendered)["qodo"]
        globs = repr(want["ignore_glob"])
        for ignore in ("ignore = 'x'\n", f"ignore = {globs}\n", f"[[ignore]]\nglob = {globs}\n"):
            with self.subTest(ignore=ignore):
                text = ignore + "[config]\ndisable_auto_feedback = false\n"
                got = agent_policy.judge_toml(want)(text)
                self.assertEqual([ok for ok, _ in got], [True, False])

    def test_rulesets_need_no_manifest(self) -> None:
        with mock.patch.object(agent_policy, "carried_globs", side_effect=AssertionError("read")):
            agent_policy.rulesets(None, "Fixture", "release")

    def test_the_globs_are_the_manifest_s_verbatim_trees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "files.json"
            manifest.write_text(
                json.dumps(
                    {
                        "trees": [
                            {"target": "a/b", "fidelity": "verbatim-tree"},
                            {"target": "c", "fidelity": "intent"},
                            {"target": "d", "fidelity": "verbatim-tree", "appliesTo": ["x"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(agent_policy.carried_globs(manifest), ["a/b/**", "d/**"])

    def test_the_hub_s_own_files_hold_every_required_exclusion(self) -> None:
        rendered = agent_policy.render(None, "Fixture", "release")
        files = {
            ".github/agent-policy.json": agent_policy.serialize(rendered),
            ".coderabbit.yaml": (ROOT / ".coderabbit.yaml").read_text(encoding="utf-8"),
            ".pr_agent.toml": (ROOT / ".pr_agent.toml").read_text(encoding="utf-8"),
        }
        got = agent_policy.check(rendered, lambda path: files[path])
        lists = [r.msg for r in got if "path_filters" in r.msg or "ignore.glob" in r.msg]
        self.assertEqual(len(lists), 2)
        self.assertEqual([m for m in lists if not next(r.ok for r in got if r.msg == m)], [])

    def test_check_fails_each_file_missing_an_exclusion(self) -> None:
        rendered = agent_policy.render(None, "Fixture", "release")
        files = {
            ".github/agent-policy.json": agent_policy.serialize(rendered),
            ".coderabbit.yaml": (
                "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^develop$']\n"
                "    auto_pause_after_reviewed_commits: 0\n  path_filters: ['!docs/**']\n"
            ),
            ".pr_agent.toml": "[config]\ndisable_auto_feedback = false\n",
        }
        failed = [r.msg for r in agent_policy.check(rendered, lambda path: files[path]) if not r.ok]
        self.assertEqual(
            failed,
            [
                ".coderabbit.yaml reviews.path_filters includes ['!.github/skills/**', '!lib/fleet/sh/**']",
                ".pr_agent.toml ignore.glob includes ['.github/skills/**', 'lib/fleet/sh/**']",
            ],
        )


def agent_policy_yaml_load(text: str) -> dict[str, dict[str, dict[str, object]]]:
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(text)


class CheckCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rendered = agent_policy.render(None, "Fixture", "release")
        self.files = {
            ".github/agent-policy.json": agent_policy.serialize(self.rendered),
            ".coderabbit.yaml": (
                "reviews:\n  auto_review:\n    enabled: true\n    base_branches:\n      - ^develop$\n"
                "    auto_pause_after_reviewed_commits: 0\n"
                "  path_filters:\n    - '!.github/skills/**'\n    - '!lib/fleet/sh/**'\n"
            ),
            ".pr_agent.toml": QODO_GOOD,
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
        drifted[".pr_agent.toml"] = QODO_GOOD.replace("= false", "= true")
        self.assertEqual(len(self.failures(drifted)), 3)

    def test_base_branches_compare_as_a_set(self) -> None:
        rendered = agent_policy.render(
            registry(coderabbit={"bases": ["@default", "^a$", "^b$"]}), "Fixture", "release"
        )
        files = dict(self.files)
        files[".github/agent-policy.json"] = agent_policy.serialize(rendered)
        files[".coderabbit.yaml"] = (
            "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^b$', '^a$']\n"
            "    auto_pause_after_reviewed_commits: 0\n  path_filters: ['!.github/skills/**', '!lib/fleet/sh/**']\n"
        )
        got = agent_policy.check(rendered, lambda path: files[path])
        self.assertTrue(all(r.ok for r in got), got)

    def test_a_coderabbit_file_that_pauses_fails(self) -> None:
        for pause in (
            "",
            "    auto_pause_after_reviewed_commits: 5\n",
            "    auto_pause_after_reviewed_commits: false\n",
        ):
            with self.subTest(pause=pause):
                files = dict(self.files)
                files[".coderabbit.yaml"] = (
                    "reviews:\n  path_filters: ['!.github/skills/**', '!lib/fleet/sh/**']\n  auto_review:\n"
                    "    enabled: true\n    base_branches:\n      - ^develop$\n" + pause
                )
                (msg,) = self.failures(files)
                self.assertIn("auto_pause_after_reviewed_commits is 0", msg)

    def test_an_absent_qodo_key_fails(self) -> None:
        files = dict(self.files)
        files[".pr_agent.toml"] = (
            "[ignore]\nglob = ['.github/skills/**', 'lib/fleet/sh/**']\n[config]\nmodel = 'x'\n"
        )
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
        self.assert_standard_library_only(["facts"])

    def test_the_rulesets_path_imports_nothing_third_party(self) -> None:
        """configure.sh runs it under a bare interpreter rather than uv."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assert_standard_library_only(["rulesets", "--out-dir", tmp])

    def assert_standard_library_only(self, command: list[str]) -> None:
        argv = ["agent_policy.py", command[0], "--registry", "none.json", "--name", "X"]
        argv += ["--model", "release", *command[1:]]
        code = (
            "import sys, runpy;"
            f"sys.argv={argv!r};"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r});"
            "import agent_policy, pr_review_policy;"
            "rc=agent_policy.main(sys.argv[1:]);"
            "bad=[m for m in ('ruamel','tomlkit','markdown_it','yaml','editorconfig') if m in sys.modules];"
            "sys.exit(1 if bad or rc else 0)"
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
            ".coderabbit.yaml": (
                "reviews:\n  auto_review:\n    enabled: true\n    base_branches: ['^develop$']\n"
                "    auto_pause_after_reviewed_commits: 0\n"
                "  path_filters:\n    - '!.github/skills/**'\n    - '!lib/fleet/sh/**'\n"
            ),
            ".pr_agent.toml": QODO_GOOD,
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

    def test_a_malformed_registry_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.json"
            path.write_text("{ not json", encoding="utf-8")
            done = self.run_cli(
                "render", "--registry", str(path), "--name", "Fixture", "--model", "release"
            )
        self.assertEqual(done.returncode, 1)
        self.assertIn("unreadable", done.stderr)


class RulesetsCase(unittest.TestCase):
    """The ruleset payloads configure.sh applies and spec/audit.py expects, composed per repository."""

    def types(self, data: object, model: str = "release") -> dict[str, list[str]]:
        got = agent_policy.rulesets(data, "Fixture", model)
        return {branch: [r["type"] for r in payload["rules"]] for branch, payload in got.items()}

    @staticmethod
    def entry(**fields: object) -> dict[str, object]:
        return {"repos": [{"name": "Fixture", **fields}, {"name": "Other"}]}

    def test_the_default_keeps_copilot_on_main_and_the_check_on_both(self) -> None:
        got = self.types(registry())
        self.assertEqual(got["main"].count("copilot_code_review"), 1)
        self.assertNotIn("copilot_code_review", got["develop"])
        for branch in ("main", "develop"):
            self.assertEqual(got[branch].count("required_status_checks"), 1)

    def test_a_wanted_copilot_rule_missing_from_main_is_refused(self) -> None:
        """A main.json regenerated without the rule would otherwise strip it fleet-wide unseen."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            main = json.loads((ROOT / "repo-config" / "main.json").read_text(encoding="utf-8"))
            main["rules"] = [r for r in main["rules"] if r["type"] != "copilot_code_review"]
            (config / "main.json").write_text(json.dumps(main), encoding="utf-8")
            shutil.copy(ROOT / "repo-config" / "develop.json", config / "develop.json")
            with self.assertRaises(agent_policy.Refusal):
                agent_policy.rulesets(registry(), "Fixture", "release", config)
            explicit = registry(copilot={"trigger": "explicit"})
            got = agent_policy.rulesets(explicit, "Fixture", "release", config)
            self.assertNotIn("copilot_code_review", [r["type"] for r in got["main"]["rules"]])

    def test_the_committed_payloads_compose_for_every_registry_entry(self) -> None:
        """A regenerated payload missing a rule fails here, on the pull request that commits it."""
        data = json.loads((ROOT / "registry" / "repos.json").read_text(encoding="utf-8"))
        default = data.get("defaults", {}).get("workflowModel", "release")
        for entry in data["repos"]:
            model = entry.get("workflowModel", default)
            with self.subTest(name=entry["name"]):
                agent_policy.rulesets(data, entry["name"], model)
        for model in agent_policy.MODELS:
            with self.subTest(model=model):
                agent_policy.rulesets({"repos": []}, "Fixture", model)

    def test_a_kept_check_missing_from_its_payload_is_refused(self) -> None:
        """A payload regenerated without the check would otherwise strip it fleet-wide unseen."""
        for model, branch, rel in (
            ("release", "main", "main.json"),
            ("release", "develop", "develop.json"),
            ("operational", "main", "main.json"),
        ):
            with self.subTest(model=model, branch=branch), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp)
                shutil.copytree(ROOT / "repo-config", config, dirs_exist_ok=True)
                payload = json.loads((config / rel).read_text(encoding="utf-8"))
                payload["rules"] = [
                    r for r in payload["rules"] if r["type"] != "required_status_checks"
                ]
                (config / rel).write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(agent_policy.Refusal):
                    agent_policy.rulesets(registry(), "Fixture", model, config)
                dropped = {"repos": [{"name": "Fixture", "pullRequestCheck": False}]}
                got = agent_policy.rulesets(dropped, "Fixture", model, config)
                self.assertNotIn(
                    "required_status_checks", [r["type"] for r in got[branch]["rules"]]
                )

    def test_copilot_follows_the_render(self) -> None:
        self.assertNotIn(
            "copilot_code_review", self.types(registry(copilot={"trigger": "explicit"}))["main"]
        )
        both = self.types(registry(copilot={"bases": ["@default", "^develop$"]}))
        self.assertEqual(both["develop"].count("copilot_code_review"), 1)

    def test_a_declared_false_drops_the_check_and_nothing_else(self) -> None:
        kept = self.types(self.entry())
        dropped = self.types(self.entry(pullRequestCheck=False))
        for branch in ("main", "develop"):
            with self.subTest(branch=branch):
                want = [t for t in kept[branch] if t != "required_status_checks"]
                self.assertEqual(dropped[branch], want)

    def test_only_a_declared_false_drops_the_check(self) -> None:
        other = {"repos": [{"name": "Fixture"}, {"name": "Other", "pullRequestCheck": False}]}
        cases: list[object] = [self.entry(pullRequestCheck=True), other, {"repos": []}, None]
        for data in cases:
            with self.subTest(data=data):
                self.assertIn("required_status_checks", self.types(data)["main"])

    def test_the_check_is_never_added_to_a_payload_without_one(self) -> None:
        for check in (True, False):
            with self.subTest(check=check):
                got = self.types(self.entry(pullRequestCheck=check), "operational")
                self.assertNotIn("required_status_checks", got["develop"])

    def test_the_cli_writes_both_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.json"
            path.write_text(json.dumps(self.entry(pullRequestCheck=False)), encoding="utf-8")
            out = Path(tmp) / "fresh" / "dir"
            code = agent_policy.main(
                [
                    "rulesets", "--registry", str(path), "--name", "Fixture",
                    "--model", "release", "--out-dir", str(out),
                ]
            )  # fmt: skip
            self.assertEqual(code, 0)
            for branch in ("main", "develop"):
                payload = json.loads((out / f"{branch}.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["name"], branch)
                types = [r["type"] for r in payload["rules"]]
                self.assertNotIn("required_status_checks", types)


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
