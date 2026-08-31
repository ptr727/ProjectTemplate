#!/usr/bin/env python3
"""Exercise canonical_review.py's unit model, ledger, and gate against real manifests and git trees.

Two kinds of case live here. The unit-model and ledger cases run against crafted inputs, because
the properties they assert are about text and JSON rather than about git. The gate cases build a
throwaway repository with real remote-tracking refs, for `test_local_review.py`'s reason: the
mechanism rests on git's own merge-base and blob reading rather than on anything this module could
stub convincingly. A third, smaller set reads this repository's own tree, since a unit table that
silently stops covering a canonical reports exactly what a healthy one does.

Run as `python3 scripts/tests/test_canonical_review.py`, or under
`python3 -m unittest discover -s scripts/tests`.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "spec"))
import audit
import canonical_review as cr


def run(cwd: Path, *args: str) -> str:
    """A checked git call for test setup, loud on failure so a broken fixture is never silent."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


class UnitModelCase(unittest.TestCase):
    """What a reviewer is handed, and what changes when the content does."""

    def test_a_fenced_heading_does_not_split_a_unit(self) -> None:
        """A `## ` shown in a code sample is being displayed rather than used."""
        text = "intro\n\n## Real\n\nbody\n\n```sh\n## Not a heading\n```\n\nmore\n"
        units = cr.file_units("D.md", text)
        self.assertEqual(sorted(units), ["D.md > (preamble)", "D.md > Real"])
        self.assertIn("## Not a heading", units["D.md > Real"])

    def test_the_split_agrees_with_the_fidelity_reader(self) -> None:
        """One document read two ways by two tools is the failure the shared fence step ends.

        `spec/audit.py` locates a named section for the fidelity hash. This module locates every
        section for the review unit. A case that only exercised this module would pass while the
        two disagreed about where a section stops, which is precisely the state that lets drift
        hide after a fenced sample.
        """
        text = "intro\n\n## Alpha\n\na body\n\n```\n## Fenced\n```\n\ntail\n\n## Beta\n\nb body\n"
        units = cr.file_units("D.md", text)
        for heading in ("Alpha", "Beta"):
            self.assertEqual(
                units[f"D.md > {heading}"],
                audit.extract_section(text, heading),
                f"the two readers disagree about where '{heading}' stops",
            )

    def test_a_document_with_no_level_two_heading_is_one_unit(self) -> None:
        """Naming its only region a preamble would claim a structure the document does not have."""
        self.assertEqual(sorted(cr.file_units("D.md", "# Title\n\nbody\n")), ["D.md"])

    def test_content_before_the_first_heading_is_a_unit(self) -> None:
        """A carrier reads it like any other text, so it is a unit rather than a gap."""
        units = cr.file_units("D.md", "# Title\n\nintro\n\n## One\n\nbody\n")
        self.assertEqual(sorted(units), ["D.md > (preamble)", "D.md > One"])
        self.assertIn("intro", units["D.md > (preamble)"])

    def test_a_document_opening_on_a_heading_has_no_preamble_unit(self) -> None:
        """An empty region is nothing a reviewer could read, so it is not offered as a unit."""
        self.assertEqual(sorted(cr.file_units("D.md", "## One\n\nbody\n")), ["D.md > One"])

    def test_two_sections_of_one_name_refuse(self) -> None:
        """Two answers to one question, where keeping the last records one read as covering both."""
        with self.assertRaises(cr.CannotRun) as caught:
            cr.file_units("D.md", "## Same\n\na\n\n## Same\n\nb\n")
        self.assertIn("two level-two sections", str(caught.exception))

    def test_a_non_markdown_canonical_is_one_unit(self) -> None:
        """A config file has no section seam, so the file is what a reviewer reads whole."""
        self.assertEqual(sorted(cr.file_units("c.json", '{"a": 1}\n')), ["c.json"])

    def test_the_digest_neutralizes_line_endings_and_nothing_else(self) -> None:
        crlf = "## One\r\n\r\nbody\r\n"
        lf = "## One\n\nbody\n"
        self.assertEqual(cr.digest(crlf), cr.digest(lf))
        self.assertNotEqual(cr.digest(lf), cr.digest("## One\n\nbody!\n"))

    def test_re_casing_a_heading_changes_its_unit(self) -> None:
        """The heading line's own bytes are inside the region, so a re-cased heading is a re-read."""
        first = cr.file_units("D.md", "## Verification Discipline\n\nbody\n")
        second = cr.file_units("D.md", "## verification discipline\n\nbody\n")
        self.assertNotEqual(list(first.values()), list(second.values()))

    def test_the_authored_source_of_the_generated_skills_tree(self) -> None:
        """A defect in a carried skill is fixed at its hand-authored path, never in the copy."""
        self.assertEqual(cr.authored_source({"source": cr.GENERATED_SKILLS}), cr.AUTHORED_SKILLS)
        self.assertEqual(cr.authored_source({"source": "other/tree"}), "other/tree")

    def test_unit_pairs_require_the_digest_that_was_read(self) -> None:
        self.assertEqual(cr.parse_pairs(["A.md > B=sha256:aa"]), {"A.md > B": "sha256:aa"})
        for bad in ("A.md", "=sha256:aa", "A.md="):
            with self.assertRaises(cr.CannotRun):
                cr.parse_pairs([bad])
        with self.assertRaises(cr.CannotRun):
            cr.parse_pairs(["A.md=sha256:aa", "A.md=sha256:bb"])


class RepoCase(unittest.TestCase):
    """A throwaway repository carrying a small manifest, with origin/develop planted by hand.

    The remote ref is planted with update-ref rather than by fetching a second repository, so the
    fixture needs no network. The manifest declares one entry of each disposition the selector has
    to tell apart, which is what lets a case assert that the excluded ones stay excluded.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.isolate_git_config()
        run(self.tmp, "init", "--initial-branch=develop", ".")
        run(self.tmp, "config", "user.email", "test@example.invalid")
        run(self.tmp, "config", "user.name", "Test")
        run(self.tmp, "config", "commit.gpgsign", "false")
        self.write(
            "spec/files.json",
            json.dumps(
                {
                    "trees": [
                        {
                            "source": cr.GENERATED_SKILLS,
                            "fidelity": "verbatim-tree",
                            "include": ["**/*"],
                        }
                    ],
                    "baseline": [
                        {"path": "DOC.md", "fidelity": "intent", "whole": True},
                        {"path": "CONF.json", "fidelity": "verbatim", "whole": True},
                        {"path": "IFACE.yml", "fidelity": "interface"},
                        {"path": "OWN.md"},
                        {
                            "path": "SECT.md",
                            "fidelity": "intent",
                            "sections": [
                                {"name": "Carried", "fidelity": "verbatim"},
                                "Also Carried",
                                "Not There",
                            ],
                        },
                        {"path": "GONE.md", "fidelity": "verbatim"},
                    ],
                },
                indent=2,
            )
            + "\n",
        )
        self.write("DOC.md", "intro\n\n## Alpha\n\na body\n\n## Beta\n\nb body\n")
        self.write("CONF.json", '{"a": 1}\n')
        self.write("IFACE.yml", "on: push\n")
        self.write("OWN.md", "## Mine\n\nlocal\n")
        self.write(
            "SECT.md", "intro\n\n## Carried\n\na\n\n## Also Carried\n\nb\n\n## Hub Only\n\nc\n"
        )
        self.write(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md", "# Demo\n\n## Use It\n\nhow\n")
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", "base")
        head = run(self.tmp, "rev-parse", "HEAD").strip()
        run(self.tmp, "update-ref", "refs/remotes/origin/develop", head)
        run(self.tmp, "checkout", "-b", "task")
        self.prev = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.prev)

    def isolate_git_config(self) -> None:
        """Keep the host's own git configuration out of every case."""
        empty = self.outside / "empty-gitconfig"
        empty.write_text("", encoding="utf-8")
        for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            prev = os.environ.get(name)
            os.environ[name] = str(empty)
            self.addCleanup(self.restore_env, name, prev)

    @staticmethod
    def restore_env(name: str, prev: str | None) -> None:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev

    def write(self, rel: str, text: str) -> None:
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def quiet(self, argv: list[str]) -> int:
        """Run the CLI with its output captured, for a case asserting only the exit code."""
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return cr.main(argv)

    def loud(self, argv: list[str]) -> tuple[int, str]:
        """Run the CLI and return its exit code with everything it wrote."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = cr.main(argv)
        return code, out.getvalue() + err.getvalue()

    def units(self) -> dict[str, str]:
        current, _ = cr.units(self.tmp)
        return current

    def record(self, unit: str, findings: int = 0) -> int:
        return self.quiet(
            [
                "record",
                "--reviewer",
                "agent-skill",
                "--findings",
                str(findings),
                "--unit",
                f"{unit}={self.units()[unit]}",
            ]
        )


class ManifestCase(RepoCase):
    def test_only_hub_authored_fidelity_contributes_units(self) -> None:
        """An interface contract's body is the repository's own, so there is no hub text to re-read."""
        units = self.units()
        self.assertIn("DOC.md > Alpha", units)
        self.assertIn("CONF.json", units)
        self.assertNotIn("IFACE.yml", units)
        self.assertNotIn("OWN.md > Mine", units)

    def test_the_skills_tree_is_read_at_its_authored_path(self) -> None:
        """The manifest declares the generated tree, and the unit names the file a fix may edit."""
        self.assertIn(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md > Use It", self.units())

    def test_a_sectioned_entry_carries_its_declared_sections_and_no_others(self) -> None:
        """A section this hub keeps for itself is read by no carrier, so demanding a pass on it
        would be this tool inventing an obligation the manifest does not state."""
        units = self.units()
        self.assertIn("SECT.md > Carried", units)
        self.assertIn("SECT.md > Also Carried", units)
        self.assertNotIn("SECT.md > Hub Only", units)
        self.assertNotIn("SECT.md > (preamble)", units)

    def test_a_declared_section_the_file_lacks_is_reported(self) -> None:
        """A manifest naming a heading that is not there, which is not the same as an absent file."""
        _, absent = cr.units(self.tmp)
        self.assertIn("SECT.md > Not There", absent)

    def test_a_declared_path_this_repo_does_not_hold_is_reported(self) -> None:
        """Dropping it silently is the narrowing a gate is supposed to make loud."""
        _, absent = cr.units(self.tmp)
        self.assertIn("GONE.md", absent)


class GateCase(RepoCase):
    def test_a_branch_changing_nothing_carried_is_covered(self) -> None:
        self.write("unrelated.txt", "hello\n")
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", "unrelated")
        self.assertEqual(self.quiet(["check"]), cr.EXIT_COVERED)

    def test_a_changed_unit_with_no_pass_is_refused(self) -> None:
        """The gate watched failing: this is the case the whole mechanism exists to produce."""
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("DOC.md > Alpha", output)
        self.assertNotIn("DOC.md > Beta", output, "an untouched section was demanded")

    def test_a_recorded_pass_covers_the_changed_unit(self) -> None:
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        self.assertEqual(self.record("DOC.md > Alpha"), cr.EXIT_COVERED)
        self.assertEqual(self.quiet(["check"]), cr.EXIT_COVERED)

    def test_editing_the_unit_again_retires_its_pass(self) -> None:
        """Coverage is over content, so a pass cannot outlive the text the reviewer read."""
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited twice\n\n## Beta\n\nb body\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("stale", output)
        self.assertIn(
            f"DOC.md > Alpha={self.units()['DOC.md > Alpha']}",
            output,
            "the refusal must print the digest record takes, not a truncation",
        )

    def test_editing_a_neighbour_leaves_a_covered_unit_covered(self) -> None:
        """The read of this section is still a read of these bytes, which is what keeps it usable."""
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body, edited\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("DOC.md > Beta", output)
        self.assertNotIn("DOC.md > Alpha", output)

    def test_a_file_this_branch_adds_owes_every_one_of_its_units(self) -> None:
        """Which is exactly what a first carrier of it faces."""
        self.write(f"{cr.AUTHORED_SKILLS}/fresh/SKILL.md", "# F\n\n## One\n\na\n\n## Two\n\nb\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("fresh/SKILL.md > One", output)
        self.assertIn("fresh/SKILL.md > Two", output)

    def manifest(self) -> dict:
        return json.loads((self.tmp / "spec/files.json").read_bytes().decode("utf-8"))

    def test_newly_carrying_an_existing_file_is_a_changed_unit(self) -> None:
        """The branch changes nothing in the file and everything about who reads it.

        Measured against this branch's own manifest on both sides, the digests match and the gate
        reports no change, so content nothing has read here reaches every carrier unreviewed. That
        is the exact first-read case the gate exists for, which is why the base is resolved against
        the base commit's own manifest.
        """
        spec = self.manifest()
        for entry in spec["baseline"]:
            if entry["path"] == "OWN.md":
                entry["fidelity"] = "verbatim"
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("OWN.md > Mine", output)

    def test_newly_declaring_a_section_is_a_changed_unit(self) -> None:
        """The same hazard one level down: the file was carried, this section was not."""
        spec = self.manifest()
        for entry in spec["baseline"]:
            if entry["path"] == "SECT.md":
                entry["sections"].append("Hub Only")
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("SECT.md > Hub Only", output)
        self.assertNotIn("SECT.md > Carried", output, "an already-carried section was demanded")

    def test_a_base_that_predates_the_manifest_makes_every_unit_a_first_read(self) -> None:
        """Nothing was carried then, so nothing about this branch's units has been read."""
        (self.tmp / "spec/files.json").unlink()
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", "drop the manifest")
        run(
            self.tmp,
            "update-ref",
            "refs/remotes/origin/develop",
            run(self.tmp, "rev-parse", "HEAD").strip(),
        )
        run(self.tmp, "revert", "--no-edit", "HEAD")
        code, output = self.loud(["check"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("DOC.md > Alpha", output)

    def test_a_commit_sha_target_resolves(self) -> None:
        """The form the pull request check passes, which is a bare commit rather than a branch."""
        base = run(self.tmp, "rev-parse", "refs/remotes/origin/develop").strip()
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        code, output = self.loud(["check", "--target", base])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("DOC.md > Alpha", output)

    def test_an_unresolvable_target_cannot_run_rather_than_reporting_everything_new(self) -> None:
        """Every path of an unresolvable ref reads as absent, which would look like a new canon."""
        code, output = self.loud(["check", "--target", "no-such-branch"])
        self.assertEqual(code, cr.EXIT_CANNOT_RUN)
        self.assertIn("no-such-branch", output)

    def test_a_path_holding_a_line_ending_cannot_run(self) -> None:
        """git cat-file --batch is line-delimited, so such a path would shift every later answer
        onto the wrong request and read as content rather than as a failure."""
        with self.assertRaises(cr.CannotRun) as caught:
            cr.blobs_at(self.tmp, "HEAD", ["DOC.md", "bad\nname.md"])
        self.assertIn("line ending", str(caught.exception))

    def test_a_manifest_path_that_escapes_the_root_cannot_run(self) -> None:
        """Held to what a carried tree's own paths already are, rather than read straight off the manifest."""
        spec = json.loads((self.tmp / "spec/files.json").read_bytes().decode("utf-8"))
        spec["baseline"].append({"path": "../outside.md", "fidelity": "verbatim"})
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        with self.assertRaises(cr.CannotRun):
            cr.units(self.tmp)

    def test_an_empty_target_value_cannot_run(self) -> None:
        """A hook written as `--target "$VAR"` with the variable unset must not gate the wrong branch."""
        self.assertEqual(self.quiet(["check", "--target", ""]), cr.EXIT_CANNOT_RUN)


class LedgerCase(RepoCase):
    def test_an_absent_ledger_reads_as_nothing_recorded(self) -> None:
        self.assertEqual(cr.read_ledger(self.tmp), {})

    def test_an_unreadable_ledger_is_a_boundary_rather_than_an_empty_one(self) -> None:
        """Reading it as empty would report every unit as never reviewed, which is a verdict."""
        self.write(cr.LEDGER, "{not json")
        with self.assertRaises(cr.CannotRun):
            cr.read_ledger(self.tmp)

    def test_a_ledger_with_no_passes_list_is_a_boundary(self) -> None:
        self.write(cr.LEDGER, json.dumps({"note": "x"}) + "\n")
        with self.assertRaises(cr.CannotRun):
            cr.read_ledger(self.tmp)

    def test_two_entries_for_one_unit_refuse(self) -> None:
        """A repeated key in a lookup table is two answers to one question."""
        self.write(
            cr.LEDGER,
            json.dumps(
                {
                    "passes": [
                        {"unit": "DOC.md > Alpha", "digest": "sha256:aa"},
                        {"unit": "DOC.md > Alpha", "digest": "sha256:bb"},
                    ]
                }
            )
            + "\n",
        )
        with self.assertRaises(cr.CannotRun):
            cr.read_ledger(self.tmp)

    def test_a_check_reports_an_unreadable_ledger_rather_than_a_verdict(self) -> None:
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited\n\n## Beta\n\nb body\n")
        self.write(cr.LEDGER, "{not json")
        self.assertEqual(self.quiet(["check"]), cr.EXIT_CANNOT_RUN)

    def test_recording_twice_keeps_one_entry_per_unit(self) -> None:
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited again\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        entries = cr.read_ledger(self.tmp)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries["DOC.md > Alpha"]["digest"], self.units()["DOC.md > Alpha"])

    def test_a_recorded_pass_names_the_hub_commit_it_ran_from(self) -> None:
        """A verdict carrying no commit cannot be re-run, per GOVERNANCE.md "Hub-Hosted Tooling"."""
        self.record("DOC.md > Alpha")
        entry = cr.read_ledger(self.tmp)["DOC.md > Alpha"]
        self.assertEqual(entry["hubCommit"], run(self.tmp, "rev-parse", "HEAD").strip())
        self.assertEqual(entry["reviewer"], "agent-skill")

    def test_record_refuses_a_digest_the_content_has_moved_past(self) -> None:
        """The guard against a format-on-save between the review and the record."""
        code, output = self.loud(
            ["record", "--reviewer", "agent-skill", "--unit", "DOC.md > Alpha=sha256:00"]
        )
        self.assertEqual(code, cr.EXIT_CANNOT_RUN)
        self.assertIn("moved", output)
        self.assertFalse((self.tmp / cr.LEDGER).exists(), "a refused record still wrote a ledger")

    def test_record_refuses_a_unit_that_does_not_exist(self) -> None:
        code, output = self.loud(
            ["record", "--reviewer", "agent-skill", "--unit", "NOPE.md=sha256:00"]
        )
        self.assertEqual(code, cr.EXIT_CANNOT_RUN)
        self.assertIn("no such carried canonical unit", output)

    def test_record_refuses_a_headless_reviewer(self) -> None:
        """A headless backend earns a pass by being run, and this engine runs none, so recording
        one by hand would attest to a review that produced no completion event at all."""
        headless = sorted(k for k, v in cr.REVIEWERS.items() if v["headless"])
        self.assertTrue(headless, "the backend table stopped declaring a headless reviewer")
        for name in headless:
            code, output = self.loud(
                [
                    "record",
                    "--reviewer",
                    name,
                    "--unit",
                    f"DOC.md > Alpha={self.units()['DOC.md > Alpha']}",
                ]
            )
            self.assertEqual(code, cr.EXIT_CANNOT_RUN)
            self.assertIn("headless", output)
        self.assertFalse((self.tmp / cr.LEDGER).exists())

    def test_record_refuses_a_reviewer_the_engine_does_not_know(self) -> None:
        """One vocabulary across both records, so two spellings never split one reviewer."""
        self.assertEqual(
            self.quiet(["record", "--reviewer", "someone", "--unit", "DOC.md > Alpha=sha256:00"]),
            cr.EXIT_CANNOT_RUN,
        )

    def test_an_orphaned_pass_is_reported_rather_than_dropped(self) -> None:
        """Deciding a section moved rather than vanished is a reader's call, not this tool's."""
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Renamed\n\na body\n\n## Beta\n\nb body\n")
        _, output = self.loud(["status"])
        self.assertIn("DOC.md > Alpha", json.loads(output)["orphanedPasses"])


class BoundaryCase(RepoCase):
    """A crash is the check not having run, and must never read as the not-covered verdict."""

    def test_an_unexpected_failure_reports_the_boundary_rather_than_a_verdict(self) -> None:
        """Exit 1 is what a capture point folds as "a changed unit is uncovered", so a crash that
        fell through to the interpreter's own exit 1 would report an execution boundary as a gate
        finding. `blobs_at` is the reachable case: it runs git with a timeout and catches neither
        OSError nor TimeoutExpired."""
        for boom in (OSError("no fd"), subprocess.TimeoutExpired("git", 1), RuntimeError("x")):
            with unittest.mock.patch.object(cr, "blobs_at", side_effect=boom):
                code, output = self.loud(["check"])
            self.assertEqual(code, cr.EXIT_CANNOT_RUN, f"{type(boom).__name__} read as a verdict")
            self.assertIn("unexpected failure", output)


class ReportCase(RepoCase):
    def test_recording_rewrites_the_burn_down(self) -> None:
        """A ledger and a report that disagree are two answers about the same coverage."""
        self.record("DOC.md > Alpha")
        text = (self.tmp / cr.REPORT).read_bytes().decode("utf-8")
        self.assertIn("- covered: 1", text)

    def test_the_report_names_every_outstanding_unit(self) -> None:
        self.assertEqual(self.quiet(["report"]), cr.EXIT_COVERED)
        text = (self.tmp / cr.REPORT).read_bytes().decode("utf-8")
        for unit in self.units():
            section = unit.split(cr.SECTION_DELIM, 1)[-1] if cr.SECTION_DELIM in unit else unit
            self.assertIn(section, text, f"{unit} is missing from the burn-down")

    def test_the_report_counts_a_recorded_pass(self) -> None:
        self.record("DOC.md > Alpha")
        self.quiet(["report"])
        text = (self.tmp / cr.REPORT).read_bytes().decode("utf-8")
        self.assertIn("- covered: 1", text)
        self.assertNotIn("**Alpha** -", text, "a covered unit is still listed as outstanding")


class LiveTreeCase(unittest.TestCase):
    """Read this repository's own manifest, so a table that stops covering a canonical fails loudly.

    A gate that finds nothing is indistinguishable from a gate with nothing to find, so these
    assert a floor on what a healthy run covers rather than only that a run happened.
    """

    root: Path
    units: dict[str, str]
    absent: list[str]
    manifest: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = SCRIPTS.parent
        cls.units, cls.absent = cr.units(cls.root)
        cls.manifest = json.loads((cls.root / cr.MANIFEST).read_bytes().decode("utf-8"))

    def test_every_declared_markdown_section_is_a_unit(self) -> None:
        """The manifest's own section list is the floor, so a heading rename surfaces here."""
        declared = 0
        for entry in self.manifest["baseline"]:
            if entry.get("fidelity") not in cr.AUTHORED_FIDELITY:
                continue
            path = entry["path"]
            if not path.endswith(".md") or not (self.root / path).is_file():
                continue
            for section in entry.get("sections", []):
                name = section if isinstance(section, str) else section.get("name", "")
                declared += 1
                self.assertIn(
                    f"{path}{cr.SECTION_DELIM}{name}",
                    self.units,
                    f"{path} declares '{name}' and no unit covers it",
                )
        self.assertGreaterEqual(declared, 20, "the manifest's declared sections stopped being read")

    def test_every_skill_contributes_at_least_one_unit(self) -> None:
        skills = sorted(p.parent.name for p in (self.root / cr.AUTHORED_SKILLS).glob("*/SKILL.md"))
        self.assertGreaterEqual(len(skills), 20, "the skills tree stopped being enumerated")
        for name in skills:
            prefix = f"{cr.AUTHORED_SKILLS}/{name}/SKILL.md"
            self.assertTrue(
                any(
                    unit == prefix or unit.startswith(prefix + cr.SECTION_DELIM)
                    for unit in self.units
                ),
                f"the {name} skill contributes no unit",
            )

    def test_the_carried_canonicals_each_contribute_units(self) -> None:
        """Named one by one, since a manifest edit that drops one is the silent case."""
        for path in (
            "AGENTS.md",
            "GOVERNANCE.md",
            "CODESTYLE.md",
            "WORKFLOW.md",
            "AUDIT.md",
            "CLAUDE.md",
            ".github/copilot-instructions.md",
        ):
            self.assertTrue(
                any(
                    unit == path or unit.startswith(path + cr.SECTION_DELIM) for unit in self.units
                ),
                f"{path} contributes no unit",
            )

    def test_a_hub_only_section_is_not_a_unit(self) -> None:
        """These two sections are undeclared on purpose, so no downstream copy ever reads them."""
        for name in (
            "Repository Onboarding and Conformance",
            "Running the Linters Locally (Known-Working Invocations)",
        ):
            self.assertIn(
                f"## {name}",
                (self.root / "GOVERNANCE.md").read_bytes().decode("utf-8"),
                "the fixture's premise moved: this section is gone from GOVERNANCE.md",
            )
            self.assertNotIn(f"GOVERNANCE.md{cr.SECTION_DELIM}{name}", self.units)

    def test_the_unit_count_holds_a_floor(self) -> None:
        """A table narrowing to a handful of units would otherwise pass every case above."""
        self.assertGreaterEqual(len(self.units), 200, "the unit set collapsed")


if __name__ == "__main__":
    unittest.main()
