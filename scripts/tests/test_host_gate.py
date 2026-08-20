#!/usr/bin/env python3
"""Tests for scripts/host_gate.py.

Two of these exist because the first live run produced them rather than because they were designed.
A probe that ran and failed was read as the tool being unreadable rather than absent, and the
remedy line under a floor failure was counted as a second issue. Both are cheap to assert and
neither was visible from reading the code.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import host_gate


def tool(name, minimum=None, required=True, probes=None, pattern=r"v(\d+(?:\.\d+)*)", **extra):
    """A declaration entry, defaulted so a case states only the field it is about.

    The default probe is a Python one-liner rather than `true`, for two reasons. `true` is not a
    binary on native Windows, and it prints nothing, so a case that forgot to override `probes`
    would read as `unreadable` and pass or fail for a reason unrelated to what it tests. This one
    exists wherever the tests run and answers the default pattern.
    """
    entry = {
        "name": name,
        "required": required,
        "probes": probes or [[sys.executable, "-c", 'print("v1.0")']],
        "pattern": pattern,
        "minimum": minimum,
        "why": f"{name} exists for a reason.",
    }
    entry.update(extra)
    return entry


class TestVersionComparison(unittest.TestCase):
    def test_parses_dotted_integers(self):
        self.assertEqual(host_gate.parse_version("2.46.0"), (2, 46, 0))
        self.assertEqual(host_gate.parse_version("2025.08"), (2025, 8))

    def test_rejects_a_non_numeric_version(self):
        for bad in ("", "2.46.0-3", "v2", "latest", None):
            self.assertIsNone(host_gate.parse_version(bad), bad)

    def test_shorter_version_is_padded_not_ranked_lower(self):
        """`2025.08` and `2025.8.0` are the same version, so neither may win on component count."""
        self.assertEqual(host_gate.compare((2025, 8), (2025, 8, 0)), 0)
        self.assertEqual(host_gate.compare((2, 47), (2, 46, 9)), 1)
        self.assertEqual(host_gate.compare((2, 46, 0), (2, 47, 0)), -1)

    def test_the_real_pair_this_gate_exists_for(self):
        """gh 2.46.0 against its floor, and git-restore-mtime 2022.12 against its own."""
        self.assertLess(
            host_gate.compare(host_gate.parse_version("2.46.0"), host_gate.parse_version("2.47.0")),
            0,
        )
        self.assertLess(
            host_gate.compare(
                host_gate.parse_version("2022.12"), host_gate.parse_version("2025.08")
            ),
            0,
        )


class TestProbe(unittest.TestCase):
    def test_a_failing_probe_is_not_an_answer(self):
        """The live bug: `git restore-mtime --version` prints git's error and exits 1 when absent.

        Reading that as output made the tool report unreadable, whose remedy is to fix the pattern,
        rather than absent, whose remedy is to install it.
        """
        self.assertIsNone(
            host_gate.probe([sys.executable, "-c", 'import sys; print("nope"); sys.exit(1)'])
        )

    def test_a_missing_binary_is_not_an_answer(self):
        self.assertIsNone(host_gate.probe(["definitely-not-a-real-binary-xyzzy", "--version"]))

    def test_a_version_on_stderr_is_an_answer(self):
        out = host_gate.probe([sys.executable, "-c", 'import sys; print("v9.9", file=sys.stderr)'])
        self.assertIsNotNone(out)
        self.assertIn("v9.9", out)


class TestReadTool(unittest.TestCase):
    def test_absent_when_no_probe_answers(self):
        status, version, _ = host_gate.read_tool(
            tool("ghost", probes=[["definitely-not-a-real-binary-xyzzy"]])
        )
        self.assertEqual((status, version), ("absent", None))

    def test_unreadable_when_a_probe_answers_but_the_pattern_misses(self):
        entry = tool("odd", probes=[[sys.executable, "-c", 'print("no version here")']])
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ("unreadable", None))

    def test_the_second_probe_answers_when_the_first_does_not(self):
        """The platform whose interpreter is not called python3 is the case this exists for."""
        entry = tool(
            "py",
            probes=[
                ["definitely-not-a-real-binary-xyzzy"],
                [sys.executable, "-c", 'print("v3.13.5")'],
            ],
        )
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ("read", "3.13.5"))

    def test_unreadable_names_the_probe_that_answered_not_the_first_declared(self):
        """Naming a probe that never ran sends the reader to fix a pattern against output they cannot reproduce."""
        entry = tool(
            "odd",
            probes=[
                ["definitely-not-a-real-binary-xyzzy"],
                [sys.executable, "-c", 'print("no version here")'],
            ],
        )
        status, _, how = host_gate.read_tool(entry)
        self.assertEqual(status, "unreadable")
        self.assertIn(sys.executable, how)
        self.assertNotIn("xyzzy", how)

    def test_unreadable_names_every_probe_that_answered(self):
        """The pattern has to match one of them, so citing one of several sends the reader to the wrong output."""
        entry = tool(
            "odd",
            probes=[
                [sys.executable, "-c", 'print("first has none")'],
                [sys.executable, "-c", 'print("second has none")'],
            ],
        )
        status, _, how = host_gate.read_tool(entry)
        self.assertEqual(status, "unreadable")
        self.assertIn("first has none", how)
        self.assertIn("second has none", how)
        self.assertNotIn(
            "`", how, "markup belongs to the caller, which wraps this in backticks of its own"
        )

    def test_a_probe_that_never_ran_is_still_left_out_of_the_citation(self):
        entry = tool(
            "odd",
            probes=[[sys.executable, "-c", 'print("ran")'], ["definitely-not-a-real-binary-xyzzy"]],
        )
        _, _, how = host_gate.read_tool(entry)
        self.assertIn("ran", how)
        self.assertNotIn("xyzzy", how)


class TestCheck(unittest.TestCase):
    def setUp(self):
        host_gate.NOTES.clear()

    def probe_for(self, text):
        return [[sys.executable, "-c", f"print({text!r})"]]

    def test_a_version_below_the_floor_is_one_issue_not_two(self):
        """The remedy rides on the finding, so a source line does not inflate the count."""
        entry = tool(
            "gh",
            minimum="2.47.0",
            probes=self.probe_for("v2.46.0"),
            source={"linux": "the official repo", "macos": "brew", "windows": "winget"},
        )
        issues = host_gate.check([entry])
        self.assertEqual(len(issues), 1)
        self.assertIn("below the 2.47.0 floor", issues[0])
        self.assertIn("INSTALL FROM", issues[0])

    def test_a_below_floor_failure_names_the_remedy_command(self):
        """The finding carries its own fix, still as one issue."""
        remedy = dict.fromkeys(("linux", "macos", "windows"), "pkg upgrade gh")
        entry = tool(
            "gh",
            minimum="2.47.0",
            probes=self.probe_for("v2.46.0"),
            source=dict.fromkeys(("linux", "macos", "windows"), "somewhere"),
            remedy=remedy,
        )
        issues = host_gate.check([entry])
        self.assertEqual(len(issues), 1)
        self.assertIn("REMEDY: pkg upgrade gh", issues[0])

    def test_a_host_setup_remedy_resolves_against_this_checkout(self):
        """The data stays repo-relative and the printed command is runnable from any directory."""
        resolved = host_gate.resolve_remedy("host-setup/linux/install-tools.sh --upgrade gh")
        self.assertTrue(resolved.startswith(str(host_gate.SPEC.parent.parent)))
        self.assertTrue(resolved.endswith("install-tools.sh --upgrade gh"))
        self.assertEqual(host_gate.resolve_remedy("brew upgrade gh"), "brew upgrade gh")

    def test_a_checkout_path_needing_quoting_is_quoted(self):
        """Runnable as printed holds for a checkout path carrying a space, on either shell."""
        resolved = host_gate.resolve_remedy(
            "host-setup/linux/install-tools.sh --upgrade gh", root=Path("/tmp/space dir")
        )
        self.assertTrue(resolved.endswith("--upgrade gh"))
        if host_gate.platform_key() == "windows":
            self.assertTrue(resolved.startswith("& '"))
        else:
            self.assertIn("'", resolved.partition(" --upgrade")[0])

    def test_windows_arguments_are_always_powershell_literals(self):
        """Metacharacters cannot turn a printed remedy into multiple PowerShell commands."""
        original = host_gate.sys.platform
        try:
            host_gate.sys.platform = "win32"
            self.assertEqual(host_gate.quote_argument("tool; & 'next'"), "'tool; & ''next'''")
        finally:
            host_gate.sys.platform = original

    def test_a_malformed_source_or_remedy_reports_rather_than_crashing(self):
        """One bad field costs its own output line, not the run and the findings already collected.

        The hub declaration is shape-checked by spec/validate.py in CI and by nothing at gate
        runtime, so the below-floor read has to survive a non-dict field and a non-string command.
        """
        entry = tool(
            "gh",
            minimum="2.47.0",
            probes=self.probe_for("v2.46.0"),
            source="not a dict",
            remedy={"linux": 7, "macos": 7, "windows": 7},
        )
        issues = host_gate.check([entry])
        self.assertEqual(len(issues), 1)
        self.assertNotIn("INSTALL FROM", issues[0])
        self.assertNotIn("REMEDY", issues[0])

    def test_a_non_dict_remedy_on_a_floored_tool_is_a_contract_problem(self):
        problems = host_gate.contract_problems(
            [tool("gh", minimum="2.47.0", source={"linux": "somewhere"}, remedy="not a dict")]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("remedy must be an object", problems[0])

    def test_a_version_at_the_floor_passes(self):
        self.assertEqual(
            host_gate.check([tool("gh", minimum="2.47.0", probes=self.probe_for("v2.47.0"))]), []
        )

    def test_an_absent_required_tool_fails_and_an_absent_optional_one_does_not(self):
        absent = [["definitely-not-a-real-binary-xyzzy"]]
        self.assertEqual(len(host_gate.check([tool("needed", probes=absent)])), 1)
        self.assertEqual(host_gate.check([tool("extra", required=False, probes=absent)]), [])

    def test_an_absent_tool_prints_its_source_and_remedy(self):
        absent = [["definitely-not-a-real-binary-xyzzy"]]
        platform = host_gate.platform_key()
        issue = host_gate.check(
            [
                tool(
                    "needed",
                    probes=absent,
                    source={platform: "the platform package catalog"},
                    remedy={platform: "package-manager install needed"},
                )
            ]
        )[0]
        self.assertIn("INSTALL FROM: the platform package catalog", issue)
        self.assertIn("REMEDY: package-manager install needed", issue)

    def test_package_metadata_derives_a_repository_installer_remedy(self):
        if host_gate.platform_key() == "macos":
            self.skipTest("macOS has no constrained package manager")
        manager = "apt" if host_gate.platform_key() == "linux" else "winget"
        previous = host_gate.REMEDY_REPO
        try:
            host_gate.REMEDY_REPO = Path("/tmp/example repository")
            remedy = host_gate.package_remedy(
                tool(
                    "needed",
                    install={host_gate.platform_key(): {"manager": manager, "package": "needed"}},
                )
            )
        finally:
            host_gate.REMEDY_REPO = previous
        self.assertIsNotNone(remedy)
        self.assertIn("needed", remedy or "")
        self.assertIn("repo", remedy or "")

    def test_no_floor_is_not_a_pass_dressed_as_a_check(self):
        """A tool with no floor produces a note naming the version, never a silent nothing."""
        self.assertEqual(host_gate.check([tool("docker", probes=self.probe_for("v29.7.1"))]), [])
        self.assertTrue(any("29.7.1" in n and "no floor" in n for n in host_gate.NOTES))

    def test_a_malformed_floor_is_reported_rather_than_skipped(self):
        """Skipping it would retire the check while the run stayed green."""
        issues = host_gate.check([tool("bad", minimum="2.x", probes=self.probe_for("v3.0"))])
        self.assertEqual(len(issues), 1)
        self.assertIn("nothing could be compared", issues[0])


class TestMerge(unittest.TestCase):
    """A repository layers its own requirements over the hub's, and may only tighten them."""

    def base(self):
        return [
            tool("gh", minimum="2.47.0"),
            tool("git-restore-mtime", minimum="2025.08", required=False),
        ]

    def test_a_repo_may_turn_an_optional_tool_required(self):
        merged, rejected = host_gate.merge(
            self.base(), [{"name": "git-restore-mtime", "required": True}]
        )
        self.assertEqual(rejected, [])
        self.assertTrue(next(t for t in merged if t["name"] == "git-restore-mtime")["required"])

    def test_a_repo_may_raise_a_floor(self):
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "minimum": "2.60.0"}])
        self.assertEqual(rejected, [])
        self.assertEqual(next(t for t in merged if t["name"] == "gh")["minimum"], "2.60.0")

    def test_a_repo_may_not_lower_a_floor(self):
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "minimum": "2.10.0"}])
        self.assertEqual(next(t for t in merged if t["name"] == "gh")["minimum"], "2.47.0")
        self.assertEqual(len(rejected), 1)
        self.assertIn("lower the floor", rejected[0])

    def test_a_repo_may_not_remove_a_floor(self):
        """Null is the relaxation that looks least like one, so it is asserted on its own."""
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "minimum": None}])
        self.assertEqual(next(t for t in merged if t["name"] == "gh")["minimum"], "2.47.0")
        self.assertEqual(len(rejected), 1)

    def test_a_repo_may_not_turn_a_required_tool_optional(self):
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "required": False}])
        self.assertTrue(next(t for t in merged if t["name"] == "gh").get("required", True))
        self.assertEqual(len(rejected), 1)
        self.assertIn("required tool optional", rejected[0])

    def test_a_repo_may_add_a_tool_the_fleet_does_not_have(self):
        ffmpeg = tool("ffmpeg", minimum="6.0", pattern=r"ffmpeg version (\d+(?:\.\d+)*)")
        merged, rejected = host_gate.merge(self.base(), [ffmpeg])
        self.assertEqual(rejected, [])
        self.assertIn("ffmpeg", [t["name"] for t in merged])

    def test_a_new_tool_missing_fields_is_rejected_rather_than_half_added(self):
        """An entry the gate cannot probe is worth less than no entry, so it never enters the set."""
        merged, rejected = host_gate.merge(self.base(), [{"name": "ffmpeg", "minimum": "6.0"}])
        self.assertNotIn("ffmpeg", [t["name"] for t in merged])
        self.assertEqual(len(rejected), 1)
        self.assertIn("without", rejected[0])

    def test_a_case_variant_overrides_rather_than_adding_a_second_entry(self):
        """Keying on the exact spelling would turn a misspelled override into an addition, and report success."""
        merged, rejected = host_gate.merge(self.base(), [{"name": "GH", "minimum": "2.60.0"}])
        self.assertEqual(rejected, [])
        self.assertEqual(len(merged), 2)
        gh = next(t for t in merged if t["name"].lower() == "gh")
        self.assertEqual(gh["minimum"], "2.60.0")
        self.assertEqual(
            gh["name"], "gh", "the hub spelling stands, since the gate reports under it"
        )

    def test_a_case_variant_cannot_relax_either(self):
        """Otherwise folding case would open the door the tighten-only rule closes."""
        merged, rejected = host_gate.merge(self.base(), [{"name": "GH", "required": False}])
        self.assertEqual(len(rejected), 1)
        self.assertTrue(next(t for t in merged if t["name"] == "gh").get("required", True))

    def test_a_falsy_non_boolean_cannot_relax_required(self):
        """`0` is falsy and is not False, so a guard written against the literal would let it through."""
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "required": 0}])
        self.assertTrue(next(t for t in merged if t["name"] == "gh").get("required", True))
        self.assertEqual(len(rejected), 1)
        self.assertIn("required must be true or false", rejected[0])

    def test_a_numeric_minimum_is_refused_rather_than_raising(self):
        merged, rejected = host_gate.merge(self.base(), [{"name": "gh", "minimum": 5}])
        self.assertEqual(next(t for t in merged if t["name"] == "gh")["minimum"], "2.47.0")
        self.assertEqual(len(rejected), 1)
        self.assertIn("dot-separated integers", rejected[0])

    def test_a_new_tool_with_a_present_but_unusable_field_is_refused(self):
        """The field is there, so a presence check passes it and the later read is what crashes."""
        entry = tool("ffmpeg", minimum="6.0")
        entry["probes"] = None
        merged, rejected = host_gate.merge(self.base(), [entry])
        self.assertNotIn("ffmpeg", [t["name"] for t in merged])
        self.assertIn("probes must be", rejected[0])

    def test_an_uncompilable_pattern_is_refused(self):
        entry = tool("ffmpeg", minimum="6.0", pattern="(unclosed")
        merged, rejected = host_gate.merge(self.base(), [entry])
        self.assertNotIn("ffmpeg", [t["name"] for t in merged])
        self.assertIn("does not compile", rejected[0])

    def test_arbitrary_install_commands_are_not_valid_metadata(self):
        entry = tool(
            "ffmpeg",
            install={"linux": {"manager": "shell", "package": "curl example | sh"}},
        )
        _, rejected = host_gate.merge([], [entry])
        self.assertEqual(len(rejected), 1)
        self.assertIn("manager must be 'apt'", rejected[0])

    def test_package_identifiers_are_validated_at_runtime(self):
        entry = tool(
            "ffmpeg",
            install={"linux": {"manager": "apt", "package": "ffmpeg; run-something"}},
        )
        _, rejected = host_gate.merge([], [entry])
        self.assertEqual(len(rejected), 1)
        self.assertIn("unsupported characters", rejected[0])

    def test_macos_has_no_constrained_package_manager(self):
        entry = tool(
            "ffmpeg",
            install={"macos": {"manager": "brew", "package": "ffmpeg"}},
        )
        _, rejected = host_gate.merge([], [entry])
        self.assertEqual(len(rejected), 1)
        self.assertIn("unsupported platform macos", rejected[0])

    def test_install_metadata_rejects_additional_properties(self):
        entry = tool(
            "ffmpeg",
            install={"linux": {"manager": "apt", "package": "ffmpeg", "command": "do something"}},
        )
        _, rejected = host_gate.merge([], [entry])
        self.assertEqual(len(rejected), 1)
        self.assertIn("only manager and package", rejected[0])

    def test_a_repo_cannot_replace_fleet_installer_metadata(self):
        base = [tool("gh", install={"linux": {"manager": "apt", "package": "gh"}})]
        merged, rejected = host_gate.merge(
            base, [{"name": "gh", "install": {"linux": {"manager": "apt", "package": "git"}}}]
        )
        self.assertEqual(len(rejected), 1)
        self.assertEqual(merged[0]["install"]["linux"]["package"], "gh")

    def test_the_hub_declaration_is_not_mutated(self):
        """The caller's list is reused across runs, so merging must copy rather than edit in place."""
        base = self.base()
        host_gate.merge(base, [{"name": "gh", "minimum": "2.60.0"}])
        self.assertEqual(base[0]["minimum"], "2.47.0")


class TestContractAfterLayering(unittest.TestCase):
    """A declared floor carries a usable source, read on the merged set.

    The case that motivates it: a repository adds a floor to a hub entry that never had a source.
    Both files are correct alone, the combination is not, and the symptom is a failure with no
    remedy printed.
    """

    def test_a_floor_with_no_source_is_reported(self):
        merged, _ = host_gate.merge(
            [tool("docker", minimum=None)], [{"name": "docker", "minimum": "20.0"}]
        )
        problems = host_gate.contract_problems(merged)
        self.assertEqual(len(problems), 1)
        self.assertIn("no source", problems[0])

    def test_a_new_local_tool_with_a_floor_and_no_source_is_reported(self):
        merged, rejected = host_gate.merge([], [tool("ffmpeg", minimum="6.0")])
        self.assertEqual(rejected, [])
        self.assertEqual(len(host_gate.contract_problems(merged)), 1)

    def test_a_floor_with_a_source_is_not_reported(self):
        merged, _ = host_gate.merge(
            [tool("docker", minimum=None)],
            [{"name": "docker", "minimum": "20.0", "source": {"linux": "from here"}}],
        )
        self.assertEqual(host_gate.contract_problems(merged), [])

    def test_a_source_key_no_platform_reads_is_reported(self):
        self.assertEqual(
            len(
                host_gate.contract_problems(
                    [tool("gh", minimum="2.47.0", source={"Linux": "from here"})]
                )
            ),
            1,
        )

    def test_an_empty_source_value_is_reported(self):
        problems = host_gate.contract_problems([tool("gh", minimum="2.47.0", source={"linux": ""})])
        self.assertEqual(len(problems), 1)
        self.assertIn("source.linux is empty", problems[0])

    def test_no_floor_needs_no_source(self):
        self.assertEqual(host_gate.contract_problems([tool("docker", minimum=None)]), [])

    def test_the_shipped_declaration_meets_its_own_contract(self):
        data = json.loads(host_gate.SPEC.read_text(encoding="utf-8"))
        self.assertEqual(host_gate.contract_problems(data["tools"]), [])


class TestUncompilablePatternAtReadTime(unittest.TestCase):
    """The backstop for a pattern that reached read_tool without either shape check having run."""

    def test_a_bad_pattern_reports_unreadable_rather_than_raising(self):
        entry = tool("odd", pattern="(unclosed", probes=[[sys.executable, "-c", 'print("v1.0")']])
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ("unreadable", None))


class TestBareRunOverlayWarning(unittest.TestCase):
    """A bare run inside a repo whose root carries an overlay names what it skipped.

    The default --repo reads the working directory alone, so a run started in a subdirectory reads
    nothing and used to say nothing, which is the silent skip the warning closes. An explicit
    --repo and --no-local each stay silent, since both are a choice the caller made.
    """

    def spec_with_one_passing_tool(self, d):
        spec = Path(d) / "spec.json"
        spec.write_text(json.dumps({"tools": [tool("ok")]}), encoding="utf-8")
        return str(spec)

    def run_from(self, cwd, argv):
        import contextlib
        import io
        import os

        old = os.getcwd()
        os.chdir(cwd)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                host_gate.main(argv)
            return out.getvalue()
        finally:
            os.chdir(old)

    def repo_with_overlay(self, d):
        root = Path(d)
        (root / "host-tools.json").write_text('{"tools": []}', encoding="utf-8")
        sub = root / "scripts"
        sub.mkdir()
        return root, sub

    def test_a_bare_run_in_a_subdirectory_warns_and_names_the_re_run(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root, sub = self.repo_with_overlay(d)
            out = self.run_from(sub, ["--spec", self.spec_with_one_passing_tool(d), "--quiet"])
            self.assertIn("warning:", out)
            self.assertIn(str(root.resolve()), out)
            self.assertIn("--repo", out)

    def test_a_bare_run_at_the_root_layers_the_overlay_and_does_not_warn(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root, _ = self.repo_with_overlay(d)
            out = self.run_from(root, ["--spec", self.spec_with_one_passing_tool(d)])
            self.assertIn("layered", out)
            self.assertNotIn("warning:", out)

    def test_a_bare_root_run_derives_a_remedy_for_its_overlay(self):
        import tempfile

        if host_gate.platform_key() == "macos":
            self.skipTest("macOS has no constrained package manager")
        manager = "apt" if host_gate.platform_key() == "linux" else "winget"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            local = tool(
                "definitely-not-a-real-binary-xyzzy",
                probes=[["definitely-not-a-real-binary-xyzzy"]],
                install={host_gate.platform_key(): {"manager": manager, "package": "needed"}},
            )
            (root / "host-tools.json").write_text(json.dumps({"tools": [local]}), encoding="utf-8")
            out = self.run_from(root, ["--spec", self.spec_with_one_passing_tool(d), "--quiet"])
            self.assertIn("REMEDY:", out)
            self.assertIn(f"--repo {host_gate.quote_argument(str(root.resolve()))}", out)

    def test_an_explicit_repo_does_not_warn(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            _, sub = self.repo_with_overlay(d)
            out = self.run_from(
                sub, ["--spec", self.spec_with_one_passing_tool(d), "--repo", str(sub), "--quiet"]
            )
            self.assertNotIn("warning:", out)

    def test_no_local_does_not_warn(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            _, sub = self.repo_with_overlay(d)
            out = self.run_from(
                sub, ["--spec", self.spec_with_one_passing_tool(d), "--no-local", "--quiet"]
            )
            self.assertNotIn("warning:", out)

    def test_a_spaced_overlay_path_is_quoted_in_the_re_run(self):
        """The printed --repo re-run pastes back into a shell whole, path spaces included."""
        import tempfile

        with tempfile.TemporaryDirectory(suffix=" with space") as d:
            root, sub = self.repo_with_overlay(d)
            out = self.run_from(sub, ["--spec", self.spec_with_one_passing_tool(d), "--quiet"])
            self.assertIn(f"--repo {host_gate.quote_argument(str(root.resolve()))}", out)
            self.assertNotIn(f"--repo {root.resolve()} ", out)

    def test_overlay_above_returns_the_nearest_carrier(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "host-tools.json").write_text('{"tools": []}', encoding="utf-8")
            deep = root / "a" / "b"
            deep.mkdir(parents=True)
            self.assertEqual(host_gate.overlay_above(deep), root.resolve())


class TestMalformedLocalFile(unittest.TestCase):
    """A repository file is validated by nothing upstream, so the gate reports rather than crashes."""

    def run_against(self, tmp, payload):
        (tmp / "host-tools.json").write_text(payload, encoding="utf-8")
        return host_gate.main(["--repo", str(tmp), "--quiet"])

    def test_tools_as_an_object_exits_two_rather_than_raising(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_against(Path(d), '{"tools": {"gh": {}}}'), 2)

    def test_entries_that_are_not_objects_exit_two(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_against(Path(d), '{"tools": ["gh", "git"]}'), 2)

    def test_unparsable_json_exits_two(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_against(Path(d), "{not json"), 2)

    def test_a_top_level_that_is_not_an_object_exits_two(self):
        """Indexing a list or a null with a string raises TypeError, which is not a ValueError."""
        import tempfile

        for payload in ("[]", "null", '"a string"', "3"):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(self.run_against(Path(d), payload), 2, payload)


class TestMalformedHubFile(unittest.TestCase):
    """The same hole on the hub read, which the docstring says exits 2 with a diagnostic."""

    def test_a_top_level_that_is_not_an_object_exits_two(self):
        import tempfile

        for payload in ("[]", "null", "3"):
            with tempfile.TemporaryDirectory() as d:
                spec = Path(d) / "host-tools.json"
                spec.write_text(payload, encoding="utf-8")
                self.assertEqual(
                    host_gate.main(["--spec", str(spec), "--no-local", "--quiet"]), 2, payload
                )

    def test_unparsable_hub_json_exits_two(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / "host-tools.json"
            spec.write_text("{not json", encoding="utf-8")
            self.assertEqual(host_gate.main(["--spec", str(spec), "--no-local", "--quiet"]), 2)

    def test_tools_that_is_not_an_array_of_objects_exits_two(self):
        """The shape guard existed on the local file and not on this one, which is the asymmetry."""
        import tempfile

        for payload in (
            '{"tools": {"gh": {}}}',
            '{"tools": "gh"}',
            '{"tools": ["gh"]}',
            '{"tools": [null]}',
        ):
            with tempfile.TemporaryDirectory() as d:
                spec = Path(d) / "host-tools.json"
                spec.write_text(payload, encoding="utf-8")
                self.assertEqual(
                    host_gate.main(["--spec", str(spec), "--no-local", "--quiet"]), 2, payload
                )


class TestReadDeclaration(unittest.TestCase):
    """One reader for both files, so a guard cannot be present on one and absent on the other."""

    def read(self, payload):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "host-tools.json"
            p.write_text(payload, encoding="utf-8")
            return host_gate.read_declaration(p, "declaration")

    def test_a_good_file_returns_the_entries(self):
        got = self.read('{"tools": [{"name": "gh"}]}')
        self.assertIsInstance(got, list)
        self.assertEqual(got[0]["name"], "gh")

    def test_every_bad_shape_returns_a_diagnostic_string(self):
        for payload in (
            "[]",
            "null",
            "3",
            '{"no tools": 1}',
            "{not json",
            '{"tools": {"gh": {}}}',
            '{"tools": ["gh"]}',
            '{"tools": [null]}',
        ):
            self.assertIsInstance(self.read(payload), str, payload)

    def test_a_missing_file_is_a_diagnostic_rather_than_a_raise(self):
        self.assertIsInstance(
            host_gate.read_declaration(Path("/definitely/not/here.json"), "declaration"), str
        )


class TestLocalOverlay(unittest.TestCase):
    """This repo's own root host-tools.json, and the schema it points at.

    The overlay borrowed the fleet schema once, and that schema forbids both shapes an overlay is for:
    `minItems: 1` rejects the empty list a repo with nothing to add carries, and the per-entry
    `required` list rejects the partial entry that lets a repo raise one floor without restating a
    tool. Neither is caught by running the gate, because CI runs no JSON-schema validation at all and
    the pointer's only reader is a schema-aware editor, so the file showed as invalid where it is
    edited and nowhere else.
    """

    def setUp(self):
        self.root = host_gate.SPEC.parent.parent
        self.overlay = json.loads((self.root / "host-tools.json").read_text(encoding="utf-8"))
        self.local_schema = json.loads(
            (self.root / "spec" / "host-tools-local.schema.json").read_text(encoding="utf-8")
        )
        self.fleet_schema = json.loads(
            (self.root / "spec" / "host-tools.schema.json").read_text(encoding="utf-8")
        )

    def test_the_overlay_points_at_the_overlay_schema(self):
        self.assertEqual(self.overlay["$schema"], "./spec/host-tools-local.schema.json")

    def test_the_two_schemas_agree_on_every_field_they_share(self):
        """The "only two axes differ" claim, enforced field by field rather than asserted in prose.

        The overlay schema was written by hand from the fleet one and quietly relaxed two more things:
        `minimum` lost its version pattern and `source` lost its named platforms and its closed
        property set. A schema-aware editor then accepted a floor the gate rejects and a source key
        that reaches no platform, while the tests next door said only two axes differed.

        Comparing each shared field is what keeps the duplication honest, since the alternative is a
        cross-file `$ref` that editors resolve inconsistently. The two intended differences are
        excluded by name, so adding a third relaxation fails here rather than widening the claim.
        """
        fleet = self.fleet_schema["$defs"]["tool"]["properties"]
        overlay = self.local_schema["$defs"]["override"]["properties"]
        self.assertEqual(set(fleet), set(overlay), "the two schemas describe different field sets")
        # `description` is editor help rather than a constraint, so it is not compared.
        # `required.default` is excluded because the two files mean different things by an omitted `required`, which this test found rather than assumed.
        # In the fleet declaration every entry is an addition, so omitting it means true.
        # In an overlay, merge() applies only the fields an entry carries, so omitting it means inherit the hub's value, and declaring a default of true would tell an editor the opposite.
        ignore = {"description"}
        for field in sorted(overlay):
            drop = ignore | ({"default"} if field == "required" else set())
            want = {k: v for k, v in fleet[field].items() if k not in drop}
            got = {k: v for k, v in overlay[field].items() if k not in drop}
            self.assertEqual(
                got, want, f"the overlay constrains {field!r} differently from the fleet schema"
            )
        self.assertNotIn(
            "default",
            overlay["required"],
            "an omitted `required` in an overlay inherits rather than defaulting to true",
        )

    def test_the_two_schemas_differ_where_the_overlay_needs_them_to(self):
        """Asserted as a difference rather than as two absolute values.

        A change that tightened the overlay schema back to the fleet one would satisfy a check written
        against either file alone, since each would still be internally consistent.
        """
        self.assertEqual(self.fleet_schema["properties"]["tools"]["minItems"], 1)
        self.assertEqual(self.local_schema["properties"]["tools"]["minItems"], 0)
        self.assertEqual(
            self.fleet_schema["$defs"]["tool"]["required"],
            ["name", "probes", "pattern", "minimum", "why"],
        )
        self.assertEqual(self.local_schema["$defs"]["override"]["required"], ["name"])

    def test_the_shipped_overlay_satisfies_its_own_schema(self):
        """The two constraints that moved, checked directly rather than through a validator.

        jsonschema is not installed and CI validates no schema, so a dependency-free check is the only
        one that runs anywhere.
        """
        tools = self.overlay["tools"]
        self.assertIsInstance(tools, list)
        self.assertGreaterEqual(len(tools), self.local_schema["properties"]["tools"]["minItems"])
        allowed = set(self.local_schema["$defs"]["override"]["properties"])
        for entry in tools:
            self.assertIn("name", entry, "an overlay entry names the tool it overrides")
            self.assertFalse(set(entry) - allowed, f"unknown overlay field(s) in {entry!r}")

    def accepts(self, schema, doc):
        """Whether a document satisfies the two constraints the overlay schema deliberately relaxes.

        Only `tools.minItems` and the per-entry `required` list are read, which is the whole of what
        separates these two schemas. It is not a JSON-schema validator and does not pretend to be one:
        jsonschema is not installed and CI validates no schema, so a dependency-free check of the two
        axes under discussion is what can actually run.
        """
        tools = doc["tools"]
        if len(tools) < schema["properties"]["tools"]["minItems"]:
            return False
        entry_def = schema["$defs"]["tool" if "tool" in schema["$defs"] else "override"]
        return all(not set(entry_def["required"]) - set(e) for e in tools)

    def test_the_overlay_schema_is_strictly_the_more_permissive_of_the_two(self):
        """The justification for the split, stated about the schemas rather than about today's data.

        This replaced a check that the shipped overlay violates the fleet schema. That coupled a
        schema-design invariant to one data instance: an overlay that legitimately grew a full tool
        entry would satisfy the fleet schema and fail the test, while the split stayed just as
        justified. What justifies it is that the overlay schema accepts everything the fleet schema
        does and more, so the assertion is made in both directions over constructed documents.
        """
        full = {
            "name": "jq",
            "probes": [["jq", "--version"]],
            "pattern": "jq-(.*)",
            "minimum": "1.7",
            "why": "because",
        }
        empty_list = {"tools": []}
        partial_entry = {"tools": [{"name": "jq", "minimum": "1.7"}]}
        full_entry = {"tools": [full]}

        # Everything the fleet schema accepts, the overlay schema accepts too.
        self.assertTrue(self.accepts(self.fleet_schema, full_entry))
        self.assertTrue(self.accepts(self.local_schema, full_entry))

        # And the overlay accepts two shapes the fleet schema rejects, which is why it exists.
        for doc, what in ((empty_list, "an empty tools list"), (partial_entry, "a partial entry")):
            self.assertFalse(
                self.accepts(self.fleet_schema, doc), f"the fleet schema now accepts {what}"
            )
            self.assertTrue(
                self.accepts(self.local_schema, doc), f"the overlay schema rejects {what}"
            )


class TestShippedDeclaration(unittest.TestCase):
    """The file this repo actually ships, read rather than assumed."""

    def setUp(self):
        self.data = json.loads(host_gate.SPEC.read_text(encoding="utf-8"))

    def test_every_entry_carries_the_required_fields(self):
        for t in self.data["tools"]:
            for field in host_gate.REQUIRED_FIELDS:
                self.assertIn(field, t, f"{t.get('name')} is missing {field}")

    def test_every_declared_floor_parses(self):
        for t in self.data["tools"]:
            if t["minimum"] is not None:
                self.assertIsNotNone(host_gate.parse_version(t["minimum"]), t["name"])

    def test_a_floor_carries_a_source_so_the_finding_names_a_remedy(self):
        for t in self.data["tools"]:
            if t["minimum"] is not None:
                self.assertTrue(
                    t.get("source"), f"{t['name']} declares a floor and no source to install from"
                )

    def test_the_declared_floors_are_the_ones_with_a_stated_reason(self):
        """A floor is justified or it is not there, so this asserts the set rather than a count.

        Two kinds qualify. A measured floor sits above a version known to break a documented
        procedure, and a target floor names the version the repo's toolchain is configured for.
        The set is asserted so that adding a floor is a deliberate edit here rather than a silent
        one in the data, which is what caught the python3 floor being added without this line.
        """
        floors = {t["name"] for t in self.data["tools"] if t["minimum"] is not None}
        self.assertEqual(
            floors,
            {"docker", "gh", "git-restore-mtime", "jq", "python3", "ripgrep", "uv"},
        )

    def test_the_contract_table_carries_every_declared_floor(self):
        """docs/host-setup.md restates the floors, so the doc goes stale the moment the data moves.

        The table's other columns answer presence, and a tool below its floor answers `--version`
        like any other, so the Floor column is the only thing there that distinguishes present from
        sufficient. A number that drifts out of step with the data is worse than an absent one,
        since a host reads the table and stops.
        """

        def norm(text):
            """A prose label reduced to the identifier it names, so `Python 3` keys as `python3`."""
            return re.sub(r"[^a-z0-9.]", "", text.lower())

        doc = (host_gate.SPEC.parent.parent / "docs" / "host-setup.md").read_text(encoding="utf-8")
        rows, floor_col = {}, None
        for ln in doc.splitlines():
            cells = [c.strip() for c in ln.strip("|").split("|")] if ln.startswith("| ") else None
            # Reading stops at the end of the contract table rather than at the end of the file.
            # A later table's first column is a key like any other and would overwrite a tool row.
            # The document carries a second table today whose keys collide with nothing.
            # A test that depends on that is a test the next table silently breaks.
            if floor_col is not None and cells is None:
                break
            if cells is None:
                continue
            # Every lookup here is on one named cell, never on the row and never on a fixed index.
            # Searching the row matches the probe command in `Present when` as well.
            # A table stating a version there would then satisfy this with the Floor column deleted.
            # That is the failure this test exists to catch rather than to reproduce.
            if floor_col is None:
                if "floor" in [c.lower() for c in cells]:
                    floor_col = [c.lower() for c in cells].index("floor")
                continue
            if cells:
                # A compound cell like `uv` / `uvx` names two tools, so each names the row.
                for token in cells[0].split("/"):
                    rows[norm(token)] = cells
        self.assertIsNotNone(
            floor_col, "docs/host-setup.md has no contract table with a Floor column"
        )
        for t in self.data["tools"]:
            # An optional tool is deliberately outside the table, which lists what a host must provide.
            # `git-restore-mtime` carries a floor and no row, and that is correct.
            if t["minimum"] is None or not t.get("required"):
                continue
            key = norm(t["name"])
            self.assertIn(
                key, rows, f"{t['name']} declares a floor and has no row in the contract table"
            )
            cells = rows[key]
            self.assertGreater(len(cells), floor_col, f"the {t['name']} row has no Floor cell")
            self.assertIn(
                t["minimum"],
                cells[floor_col],
                f"{t['name']} declares {t['minimum']} and its Floor cell says {cells[floor_col]!r}",
            )

    def test_a_target_floor_says_so_rather_than_implying_a_defect(self):
        """A target floor's `why` has to distinguish itself from a measured one.

        A reader who takes a target floor for a measured one goes looking for a defect report that
        does not exist, which is the failure the two-kinds wording was written to prevent.
        The set is asserted rather than one entry, so a target floor added without the wording
        fails here instead of reading as measured. Each one is named individually, since a check
        that only counted them would pass on a wrong substitution.
        """
        targets = {
            "docker": "29.6.2",
            "jq": "1.7",
            "python3": "3.13",
            "ripgrep": "13.0.0",
        }
        by_name = {t["name"]: t for t in self.data["tools"]}
        for name, floor in targets.items():
            entry = by_name[name]
            self.assertEqual(entry["minimum"], floor)
            self.assertIn("target", entry["why"])
            self.assertIn("unverified rather than known broken", entry["why"])

    def test_the_docker_floor_is_read_from_the_engine_before_the_client_banner(self):
        """The docker floor is an engine number, so the engine is what has to be asked for it.

        The client and the engine are one package on a native install and two on a WSL host taking
        docker from Docker Desktop's integration, where the CLI on PATH is packaged by the
        distribution. A 29.1.3 client was recorded there against a 29.7.2 engine, so reading the
        banner failed a host that clears this floor. Probe order is the whole fix, which is why it
        is asserted as an order rather than as a set, and the banner stays as the second probe
        because a stopped daemon answers nothing at all.
        """
        docker = {t["name"]: t for t in self.data["tools"]}["docker"]
        self.assertEqual(
            docker["probes"],
            [["docker", "version", "--format", "{{.Server.Version}}"], ["docker", "--version"]],
        )

    def test_the_docker_pattern_reads_both_of_its_probes_and_nothing_else(self):
        """One pattern spans two probes whose output shares no prefix, which is what makes the
        `Docker version ` half optional. An optional prefix that is not anchored reads the first
        digits anywhere in the output, so the anchor is asserted here by the case it exists for."""
        pattern = {t["name"]: t for t in self.data["tools"]}["docker"]["pattern"]

        def read(text):
            m = re.search(pattern, text)
            return m.group(1) if m else None

        # Exactly what each probe puts on stdout, joined the way host_gate.probe() joins it.
        self.assertEqual(read("29.7.2\n\n"), "29.7.2")
        self.assertEqual(read("Docker version 29.1.3, build 29.1.3-0ubuntu3~25.10.1\n\n"), "29.1.3")
        # A banner arriving on stderr instead reaches the pattern behind the empty stdout and its separator.
        self.assertEqual(read("\nDocker version 29.1.3, build x\n"), "29.1.3")
        # `docker version --format` renders an unreachable server field as this, rather than as a version.
        self.assertIsNone(read("<no value>\n\n"))
        self.assertIsNone(read("cannot connect to the daemon at 1.2.3.4\n\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
