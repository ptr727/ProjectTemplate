#!/usr/bin/env python3
"""Exercise scripts/central_config.py against a constructed registry and stand-in gh runners.

The script writes a central bot-settings file only while every fleet repository carries its own.
So each case pins one state of that gate, and no case reaches the network.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import central_config
from agent_policy import Refusal, Unreadable
from test_agent_policy import make_checkout

CODERABBIT = "Acme/coderabbit"
QODO = "Acme/pr-agent-settings"
GOOD_YAML = "reviews:\n  auto_review:\n    enabled: true\n"
GOOD_TOML = "[config]\ndisable_auto_feedback = false\n"


def registry(*entries: tuple[str, str]) -> dict[str, object]:
    """Registry entries as (url owner/name, status)."""
    return {
        "repos": [
            {"name": slug.split("/")[1], "url": f"https://github.com/{slug}", "status": status}
            for slug, status in entries
        ]
    }


FLEET = registry(("Acme/Widget", "cataloged"), ("Acme/Gadget", "cataloged"))


class FakeGh:
    """A gh runner answering default-branch and contents reads from a table of files."""

    def __init__(self, files: dict[str, dict[str, str | Exception]] | None = None) -> None:
        self.files = files or {}
        self.broken: set[str] = set()

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        path = argv[2]
        if path.startswith("repos/") and "/contents/" not in path:
            slug = path.removeprefix("repos/")
            return (1, "", "HTTP 500") if slug in self.broken else (0, "trunk\n", "")
        head, _, rest = path.partition("/contents/")
        slug = head.removeprefix("repos/")
        name = rest.split("?")[0]
        got = self.files.get(slug, {}).get(name)
        if isinstance(got, Exception):
            return 1, "", "gh: Server Error (HTTP 500)"
        if got is None:
            return 1, "", "gh: Not Found (HTTP 404)"
        return 0, json.dumps(base64.encodebytes(got.encode()).decode()), ""


def fleet_files(yaml_text: str | None = GOOD_YAML, toml_text: str | None = GOOD_TOML) -> FakeGh:
    files: dict[str, dict[str, str | Exception]] = {}
    for slug in ("Acme/Widget", "Acme/Gadget"):
        files[slug] = {}
        if yaml_text is not None:
            files[slug][".coderabbit.yaml"] = yaml_text
        if toml_text is not None:
            files[slug][".pr_agent.toml"] = toml_text
    return FakeGh(files)


class TableCase(unittest.TestCase):
    def test_each_config_text_sets_exactly_its_key_to_the_central_value(self) -> None:
        for name, settings in central_config.TABLE.items():
            with self.subTest(name=name):
                self.assertTrue(settings.text.endswith("\n"))
                self.assertNotIn("\r", settings.text)
                if settings.config == ".coderabbit.yaml":
                    from ruamel.yaml import YAML

                    doc = YAML(typ="safe").load(settings.text)
                else:
                    doc = tomllib.loads(settings.text)
                node = doc
                for part in settings.key[:-1]:
                    self.assertEqual(list(node), [part])
                    node = node[part]
                self.assertEqual(node, {settings.key[-1]: node[settings.key[-1]]})
                self.assertIs(node[settings.key[-1]], name == "pr-agent-settings")

    def test_every_readme_source_exists(self) -> None:
        for name in central_config.TABLE:
            with self.subTest(name=name):
                self.assertIn("# " + name, central_config.source_readme(name))

    def test_an_unlisted_repo_is_refused(self) -> None:
        for repo in ("Acme/Widget", "Acme", "/coderabbit"):
            with self.subTest(repo=repo), self.assertRaises(Refusal):
                central_config.settings_for(repo)


class PreconditionCase(unittest.TestCase):
    def holds(self, gh: FakeGh, data: object = FLEET) -> list[central_config.Result]:
        return central_config.precondition(CODERABBIT, data, gh)

    def test_it_holds_where_every_repo_sets_the_key(self) -> None:
        results = self.holds(fleet_files())
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))

    def test_each_failure_is_named(self) -> None:
        cases = {
            "absent": None,
            "unparseable": "reviews: [unclosed\n",
            "key missing": "reviews:\n  auto_review:\n    drafts: false\n",
            "non-bool": "reviews:\n  auto_review:\n    enabled: 'no'\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                gh = fleet_files(yaml_text=text)
                results = self.holds(gh)
                self.assertTrue(all(not r.ok and not r.unreadable for r in results))
                self.assertIn("Acme/Widget", results[0].msg)

    def test_one_failing_repo_fails_only_itself(self) -> None:
        gh = fleet_files()
        del gh.files["Acme/Gadget"][".coderabbit.yaml"]
        self.assertEqual([r.ok for r in self.holds(gh)], [True, False])

    def test_archived_and_other_owner_entries_are_ignored(self) -> None:
        data = registry(
            ("Acme/Widget", "cataloged"),
            ("Acme/Old", "archived"),
            ("Other/Stranger", "cataloged"),
            ("ACME/Gadget", "cataloged"),
        )
        results = self.holds(fleet_files(), data)
        self.assertEqual(len(results), 2)

    def test_excluded_entries_are_ignored_and_a_dot_git_url_reads(self) -> None:
        data = {
            "repos": [
                {"name": "Widget", "url": "https://github.com/Acme/Widget", "status": "cataloged"},
                {
                    "name": "Settings",
                    "url": "https://github.com/Acme/Settings",
                    "status": "excluded",
                },
                {
                    "name": "Gadget",
                    "url": "https://github.com/Acme/Gadget.git",
                    "status": "cataloged",
                },
            ]
        }
        results = self.holds(fleet_files(), data)
        self.assertEqual([r.ok for r in results], [True, True])

    def test_an_owner_with_no_live_repository_refuses(self) -> None:
        empty: dict[str, object] = {"repos": []}
        for data in (
            registry(("Other/Widget", "cataloged")),
            registry(("Acme/Old", "archived"), ("Acme/Settings", "excluded")),
            empty,
        ):
            with self.subTest(data=data), self.assertRaises(central_config.Refusal):
                self.holds(fleet_files(), data)

    def test_the_qodo_key_must_be_a_bool_too(self) -> None:
        gh = fleet_files(toml_text="[config]\ndisable_auto_feedback = 1\n")
        results = central_config.precondition(QODO, FLEET, gh)
        self.assertTrue(all(not r.ok for r in results))
        self.assertTrue(all(r.ok for r in central_config.precondition(QODO, FLEET, fleet_files())))

    def test_an_unreadable_read_is_marked(self) -> None:
        gh = fleet_files()
        gh.files["Acme/Widget"][".coderabbit.yaml"] = RuntimeError("x")
        self.assertTrue(self.holds(gh)[0].unreadable)
        gh = fleet_files()
        gh.broken.add("Acme/Gadget")
        self.assertTrue(self.holds(gh)[1].unreadable)

    def test_a_missing_registry_is_refused(self) -> None:
        with self.assertRaises(Refusal):
            self.holds(fleet_files(), None)


class WriteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkout = make_checkout(self.tmp.name, "https://github.com/Acme/coderabbit.git")

    def write(self, gh: FakeGh, dry_run: bool = False, repo: str = CODERABBIT) -> list[str]:
        return central_config.write(repo, FLEET, self.checkout, dry_run, gh)

    def test_the_readme_and_the_config_are_written_where_the_gate_holds(self) -> None:
        lines = self.write(fleet_files())
        self.assertEqual(lines, ["wrote README.md", "wrote .coderabbit.yaml"])
        text = central_config.TABLE["coderabbit"].text
        self.assertEqual((self.checkout / ".coderabbit.yaml").read_text(encoding="utf-8"), text)
        self.assertEqual(
            (self.checkout / "README.md").read_text(encoding="utf-8"),
            central_config.source_readme("coderabbit"),
        )

    def test_a_second_write_reports_unchanged(self) -> None:
        self.write(fleet_files())
        self.assertEqual(
            self.write(fleet_files()), ["unchanged README.md", "unchanged .coderabbit.yaml"]
        )

    def test_a_failing_gate_removes_the_config_and_says_why(self) -> None:
        self.write(fleet_files())
        gh = fleet_files()
        del gh.files["Acme/Gadget"][".coderabbit.yaml"]
        lines = self.write(gh)
        self.assertEqual(lines[1], "removed .coderabbit.yaml")
        self.assertEqual(
            lines[2],
            "withheld .coderabbit.yaml: 1 repo(s) lack their own file setting "
            "reviews.auto_review.enabled: Gadget",
        )
        self.assertFalse((self.checkout / ".coderabbit.yaml").exists())

    def test_a_failing_gate_with_no_file_writes_none(self) -> None:
        lines = self.write(fleet_files(yaml_text=None))
        self.assertEqual(lines[:2], ["wrote README.md", "unchanged .coderabbit.yaml"])
        self.assertIn("2 repo(s) lack", lines[2])
        self.assertFalse((self.checkout / ".coderabbit.yaml").exists())

    def test_a_dry_run_writes_and_removes_nothing(self) -> None:
        lines = self.write(fleet_files(), dry_run=True)
        self.assertEqual(lines, ["would write README.md", "would write .coderabbit.yaml"])
        self.assertEqual(
            sorted(p.name for p in self.checkout.iterdir() if p.is_file()), [".editorconfig"]
        )
        self.write(fleet_files())
        gh = fleet_files(yaml_text=None)
        self.assertEqual(self.write(gh, dry_run=True)[1], "would remove .coderabbit.yaml")
        self.assertTrue((self.checkout / ".coderabbit.yaml").exists())

    def test_a_wrong_origin_refuses_and_leaves_the_checkout_alone(self) -> None:
        with self.assertRaises(Refusal):
            self.write(fleet_files(), repo=QODO)
        self.assertEqual(
            sorted(p.name for p in self.checkout.iterdir() if p.is_file()), [".editorconfig"]
        )

    def test_an_unreadable_repo_stops_the_write_before_any_file_is_touched(self) -> None:
        gh = fleet_files()
        gh.broken.add("Acme/Widget")
        with self.assertRaises(Unreadable):
            self.write(gh)
        self.assertEqual(
            sorted(p.name for p in self.checkout.iterdir() if p.is_file()), [".editorconfig"]
        )

    def test_crlf_follows_the_editorconfig_for_new_files_and_the_file_for_old_ones(self) -> None:
        (self.checkout / ".editorconfig").write_text(
            "root = true\n\n[*]\nend_of_line = crlf\n", encoding="utf-8"
        )
        self.write(fleet_files())
        for name in ("README.md", ".coderabbit.yaml"):
            out = (self.checkout / name).read_bytes()
            self.assertIn(b"\r\n", out)
            self.assertNotIn(b"\n", out.replace(b"\r\n", b""))
        self.assertEqual(
            self.write(fleet_files()), ["unchanged README.md", "unchanged .coderabbit.yaml"]
        )
        (self.checkout / "README.md").write_bytes(
            central_config.source_readme("coderabbit").encode()
        )
        self.assertEqual(self.write(fleet_files())[0], "unchanged README.md")


class CheckCase(unittest.TestCase):
    def run_check(
        self, remote: dict[str, str | Exception], gh: FakeGh
    ) -> list[central_config.Result]:
        gh.files[CODERABBIT] = remote
        return central_config.check(CODERABBIT, FLEET, "main", gh)

    def readme(self) -> str:
        return central_config.source_readme("coderabbit")

    def test_a_holding_gate_wants_the_constant(self) -> None:
        text = central_config.TABLE["coderabbit"].text
        results = self.run_check(
            {"README.md": self.readme(), ".coderabbit.yaml": text}, fleet_files()
        )
        self.assertTrue(all(r.ok for r in results))
        stale = self.run_check(
            {"README.md": self.readme(), ".coderabbit.yaml": "x: 1\n"}, fleet_files()
        )
        self.assertFalse(stale[1].ok)
        missing = self.run_check({"README.md": self.readme()}, fleet_files())
        self.assertFalse(missing[1].ok)
        self.assertIn("run write", missing[1].msg)

    def test_a_failing_gate_wants_the_file_absent(self) -> None:
        gh = fleet_files(yaml_text=None)
        absent = self.run_check({"README.md": self.readme()}, gh)
        self.assertTrue(all(r.ok for r in absent))
        self.assertIn("withheld while 2 repo(s) lack their own file", absent[1].msg)
        text = central_config.TABLE["coderabbit"].text
        present = self.run_check({"README.md": self.readme(), ".coderabbit.yaml": text}, gh)
        self.assertFalse(present[1].ok)
        self.assertIn("silences CodeRabbit", present[1].msg)

    def test_readme_drift_fails_and_crlf_does_not(self) -> None:
        text = central_config.TABLE["coderabbit"].text
        drift = self.run_check({"README.md": "other\n", ".coderabbit.yaml": text}, fleet_files())
        self.assertFalse(drift[0].ok)
        crlf = self.readme().replace("\n", "\r\n")
        same = self.run_check({"README.md": crlf, ".coderabbit.yaml": text}, fleet_files())
        self.assertTrue(same[0].ok)
        gone = self.run_check({".coderabbit.yaml": text}, fleet_files())
        self.assertFalse(gone[0].ok)

    def test_an_unreadable_read_is_marked(self) -> None:
        results = self.run_check({"README.md": RuntimeError("x")}, fleet_files())
        self.assertTrue(results[0].unreadable)
        gh = fleet_files()
        gh.broken.add("Acme/Widget")
        results = self.run_check({"README.md": self.readme()}, gh)
        self.assertTrue(any(r.unreadable for r in results))


class MainCase(unittest.TestCase):
    def invoke(self, argv: list[str], gh: FakeGh) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            return central_config.main(argv, gh), out.getvalue()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "repos.json"
        self.registry.write_text(json.dumps(FLEET), encoding="utf-8")

    def argv(self, command: str, repo: str = CODERABBIT) -> list[str]:
        return [command, "--repo", repo, "--registry", str(self.registry)]

    def test_precondition_exit_codes(self) -> None:
        code, out = self.invoke(self.argv("precondition"), fleet_files())
        self.assertEqual(code, 0)
        self.assertEqual(out.count("  ok   "), 2)
        code, out = self.invoke(self.argv("precondition"), fleet_files(yaml_text=None))
        self.assertEqual((code, out.count("  FAIL ")), (1, 2))
        gh = fleet_files()
        gh.broken.add("Acme/Widget")
        self.assertEqual(self.invoke(self.argv("precondition"), gh)[0], 2)

    def test_a_refusal_exits_1_and_a_missing_registry_too(self) -> None:
        self.assertEqual(self.invoke(self.argv("precondition", "Acme/Widget"), fleet_files())[0], 1)
        argv = [
            "precondition",
            "--repo",
            CODERABBIT,
            "--registry",
            str(Path(self.tmp.name) / "none"),
        ]
        self.assertEqual(self.invoke(argv, fleet_files())[0], 1)

    def test_an_unknown_argument_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            central_config.main([*self.argv("precondition"), "--bogus-flag"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
