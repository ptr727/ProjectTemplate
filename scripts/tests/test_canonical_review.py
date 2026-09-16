#!/usr/bin/env python3
"""Exercise canonical_review.py's unit model, ledger, and sweep against real manifests and git trees.

Two kinds of case live here. The unit-model and ledger cases run against crafted inputs, because
the properties they assert are about text and JSON rather than about git. The sweep and record
cases build a throwaway repository with real remote-tracking refs, for `test_local_review.py`'s
reason: the mechanism rests on git's own file listing and merge-base rather than on anything this
module could stub convincingly. A third, smaller set reads this repository's own tree, since a unit
table that silently stops covering a canonical reports exactly what a healthy one does.

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
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=False
    )
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

    def test_two_sections_differing_only_in_case_refuse(self) -> None:
        """The declared-name lookup folds case and spec/audit.py's heading match folds, so a pair
        differing only in case is one name to every reader but this guard.

        Left unfolded, the pair passed here as two distinct keys, and `build_units`' own folded
        lookup then resolved a manifest declaring either spelling to the last of them while
        spec/audit.py resolves the same declaration to the first. A pass would be recorded over one
        section's bytes while the fidelity check hashes the other's, and the first section would be
        no unit at all."""
        text = "intro\n\n## Alpha\n\nFIRST\n\n## alpha\n\nSECOND\n"
        with self.assertRaises(cr.CannotRun):
            cr.file_units("D.md", text)

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
        self.write(".gitignore", ".DS_Store\n")
        self.write("IFACE.yml", "on: push\n")
        self.write("OWN.md", "## Mine\n\nlocal\n")
        self.write(
            "SECT.md", "intro\n\n## Carried\n\na\n\n## Also Carried\n\nb\n\n## Hub Only\n\nc\n"
        )
        self.write(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md", "# Demo\n\n## Use It\n\nhow\n")
        self.write(f"{cr.GENERATED_SKILLS}/demo/SKILL.md", "# Demo\n\n## Use It\n\nhow\n")
        # Authored-side only, the shape .agents/skills/README.md has: no repository receives it.
        self.write(f"{cr.AUTHORED_SKILLS}/README.md", "# Skills\n\n## About\n\nlocal\n")
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

    def manifest(self) -> dict:
        return json.loads((self.tmp / "spec/files.json").read_bytes().decode("utf-8"))

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

    def test_a_file_only_on_the_authored_side_is_not_a_unit(self) -> None:
        """No repository receives it, so demanding a carrier's read of it invents an obligation."""
        stem = f"{cr.AUTHORED_SKILLS}/README.md"
        offending = [u for u in self.units() if u == stem or u.startswith(stem + cr.SECTION_DELIM)]
        self.assertEqual(offending, [], "a file no repository receives became canonical content")

    def test_an_ignored_file_in_a_carried_tree_is_not_canonical_content(self) -> None:
        """A filesystem walk took whatever sat in the tree. The undecodable case is the loud one:
        it takes every subcommand to exit 2, so the local block, CI, and the sweep all stop until
        someone finds the file, and no repository carries it."""
        for tree in (cr.GENERATED_SKILLS, cr.AUTHORED_SKILLS):
            (self.tmp / tree / "demo" / ".DS_Store").write_bytes(b"\xff\xfe\x00rubbish")
        units = self.units()
        self.assertNotIn(f"{cr.AUTHORED_SKILLS}/demo/.DS_Store", units)
        self.assertIn(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md > Use It", units)

    def test_a_new_unstaged_carried_file_is_still_a_unit(self) -> None:
        """It is content a carrier will receive, so dropping it would narrow the unit set exactly
        where a new canonical is added."""
        self.write(f"{cr.GENERATED_SKILLS}/fresh/SKILL.md", "# F\n\n## One\n\na\n")
        self.write(f"{cr.AUTHORED_SKILLS}/fresh/SKILL.md", "# F\n\n## One\n\na\n")
        self.assertIn(f"{cr.AUTHORED_SKILLS}/fresh/SKILL.md > One", self.units())

    def test_a_declared_section_matches_the_heading_case_insensitively(self) -> None:
        """spec/audit.py matches case-folded, so matching exactly here would silently stop gating a
        section the fidelity check still hashes, and report it as not held at all."""
        spec = self.manifest()
        for entry in spec["baseline"]:
            if entry["path"] == "SECT.md":
                entry["sections"] = ["carried", "ALSO CARRIED"]
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        units, absent = cr.units(self.tmp)
        self.assertIn("SECT.md > Carried", units, "a re-cased declaration stopped gating a section")
        self.assertIn("SECT.md > Also Carried", units)
        self.assertNotIn("SECT.md > carried", absent)

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


class CodeSpanCase(unittest.TestCase):
    """A unit key is a heading, and a heading may name a command, so a key can hold a backtick."""

    def test_a_key_holding_a_backtick_stays_one_span(self) -> None:
        """Single backticks split such a key into two spans with the middle rendered as prose, which
        puts the path and the digest in different spans and makes the record argument uncopyable."""
        key = "doc.md > Executing a `develop -> main` promotion safely=sha256:beef"
        span = cr.code_span(key)
        self.assertTrue(span.startswith("``") and span.endswith("``"))
        self.assertEqual(span[2:-2], key, "the key must survive the fence unchanged")

    def test_a_key_touching_a_backtick_is_padded(self) -> None:
        """CommonMark strips one leading and trailing space, so the pad is what keeps the fence from
        running into the content and closing early."""
        self.assertEqual(cr.code_span("`x"), "`` `x ``")
        self.assertEqual(cr.code_span("x`"), "`` x` ``")

    def test_a_key_with_no_backtick_takes_the_plain_fence(self) -> None:
        self.assertEqual(cr.code_span("plain.md > Section"), "`plain.md > Section`")


class SweepCase(RepoCase):
    """The weekly sweep's work list: every stale unit, plus a bounded slice of the never-read backlog."""

    def cover_all(self) -> None:
        """Record a pass over every unit, so a case can start from a tree the sweep asks nothing of."""
        for unit in self.units():
            self.record(unit)

    def test_a_tree_with_every_unit_covered_asks_for_nothing(self) -> None:
        """Which is what closes the sweep's issue, and what makes the exit code worth reading."""
        self.cover_all()
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_COVERED)
        self.assertIn("0 unit(s) whose text moved past its pass", output)
        self.assertIn("0 unit(s) from the never-read backlog", output)
        # The instruction and its fence describe digests, so a body holding none must not carry them.
        self.assertNotIn("Record each pass at the digest above", output)
        self.assertNotIn("--reviewer agent-skill --unit", output)

    def test_a_never_read_unit_is_asked_for_within_the_bound(self) -> None:
        """The case the retired gate refused: a unit nothing here has read is one a carrier receives
        unread, so leaving the whole backlog out left newly authored content with no reader at all."""
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn(f"{cr.BACKLOG_SLICE} unit(s) from the never-read backlog", output)
        listed = [u for u in self.units() if f"- `{u}=" in output]
        self.assertEqual(len(listed), cr.BACKLOG_SLICE, "the slice is bounded by BACKLOG_SLICE")

    def test_the_backlog_slice_takes_the_newest_committed_first(self) -> None:
        """A unit just authored here is the one a carrier is about to receive unread, so it comes
        ahead of a unit that has been unread for months."""
        self.write("LATE.md", "## Late\n\nbody\n")
        spec = self.manifest()
        spec["baseline"].append({"path": "LATE.md", "fidelity": "verbatim", "whole": True})
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        run(self.tmp, "add", "-A")
        # An explicit committer date, since `git log --format=%ct` resolves to the second.
        # The fixture's own commit lands in that same second, so the tie-break would decide this case instead of the order under test.
        prev = os.environ.get("GIT_COMMITTER_DATE")
        os.environ["GIT_COMMITTER_DATE"] = "2099-01-01T00:00:00+00:00"
        self.addCleanup(self.restore_env, "GIT_COMMITTER_DATE", prev)
        run(self.tmp, "commit", "-m", "carry a new canonical")
        _, output = self.loud(["sweep"])
        body = output.split("never-read backlog")[1]
        self.assertIn("LATE.md > Late=", body, "the newest carried unit was not in the slice")

    def test_a_carried_file_no_commit_holds_leads_the_slice(self) -> None:
        """`tracked_files` counts a carried file this branch created and has not committed, and it is
        the newest content there is. `git log` answers for it with a zero exit and no output, which
        read as the undatable case would sort it last, the exact inverse of the documented order."""
        self.write("BRAND.md", "## Brand\n\nbody\n")
        spec = self.manifest()
        spec["baseline"].append({"path": "BRAND.md", "fidelity": "verbatim", "whole": True})
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        run(self.tmp, "add", "BRAND.md")
        _, output = self.loud(["sweep"])
        body = output.split("never-read backlog")[1]
        # The first bullet rather than membership, since the case is named for the position.
        # A value merely above the fixture's own commit time would satisfy a membership assertion.
        first = next(line for line in body.splitlines() if line.startswith("- `"))
        self.assertTrue(
            first.startswith("- `BRAND.md > Brand="),
            f"an uncommitted carried unit did not lead the slice, {first} did",
        )

    def test_a_repository_with_no_commits_dates_nothing_and_still_sweeps(self) -> None:
        """The third answer `newest_commit_times` documents. With no commit at all `git log` exits
        128 rather than answering, so every path is undatable, and the order has to degrade to the
        tie-break rather than raising out of a sweep that can otherwise run."""
        fresh = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        run(fresh, "init", "--initial-branch=develop", ".")
        (fresh / "spec").mkdir()
        (fresh / "spec/files.json").write_bytes(
            json.dumps(
                {"baseline": [{"path": "DOC.md", "fidelity": "verbatim", "whole": True}]}
            ).encode()
        )
        (fresh / "DOC.md").write_bytes(b"## Alpha\n\nbody\n")
        run(fresh, "add", "-A")
        times = cr.newest_commit_times(fresh, ["DOC.md"])
        self.assertEqual(times, {"DOC.md": 0.0}, "an unborn HEAD must not raise out of the sweep")
        self.assertEqual(cr.backlog_slice(fresh, ["DOC.md > Alpha"]), ["DOC.md > Alpha"])

    def test_every_stale_unit_is_asked_for_whatever_the_bound(self) -> None:
        """The bound is on the backlog alone. Stale text is content a carrier is receiving right now
        that a pass here has been retired from, so none of it waits for a later round.

        More stale units than `BACKLOG_SLICE`, deliberately: with fewer, applying the bound to this
        list as well would leave the case green and the property it is named for unbound.
        """
        self.cover_all()
        self.write("DOC.md", "edited\n\n## Alpha\n\nedited\n\n## Beta\n\nedited\n")
        self.write(
            "SECT.md", "edited\n\n## Carried\n\nx\n\n## Also Carried\n\ny\n\n## Hub Only\n\nc\n"
        )
        self.write("CONF.json", '{"a": 2}\n')
        self.write(f"{cr.GENERATED_SKILLS}/demo/SKILL.md", "# Demo\n\n## Use It\n\nedited\n")
        self.write(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md", "# Demo\n\n## Use It\n\nedited\n")
        ledger = cr.read_ledger(self.tmp)
        stale = sum(
            1
            for unit, digest in self.units().items()
            if cr.state_of(unit, digest, ledger) == "stale"
        )
        self.assertGreater(
            stale, cr.BACKLOG_SLICE, "the case needs more stale units than the bound"
        )
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn(f"{stale} unit(s) whose text moved past its pass", output)
        # The listing rather than the heading, since a bound applied at emission leaves the count right and the list short.
        current = self.units()
        for unit, digest in current.items():
            if cr.state_of(unit, digest, ledger) == "stale":
                self.assertIn(f"- `{unit}={digest}`", output, f"{unit} waited for a later round")

    def test_editing_a_unit_past_its_pass_puts_it_on_the_list(self) -> None:
        """The sweep watched working: this is the case the whole mechanism exists to produce."""
        self.cover_all()
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("1 unit(s) whose text moved past its pass", output)
        self.assertIn(
            f"DOC.md > Alpha={self.units()['DOC.md > Alpha']}",
            output,
            "the list must print the digest record takes, not a truncation",
        )

    def test_editing_a_neighbour_leaves_a_covered_unit_off_the_list(self) -> None:
        """The read of this section is still a read of these bytes, which is what keeps it usable."""
        self.cover_all()
        self.write("DOC.md", "intro\n\n## Alpha\n\na body\n\n## Beta\n\nb body, edited\n")
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("DOC.md > Beta=", output)
        self.assertNotIn("DOC.md > Alpha=", output)

    def test_a_fresh_pass_takes_the_unit_off_the_list(self) -> None:
        self.cover_all()
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        self.assertEqual(self.quiet(["sweep"]), cr.EXIT_NOT_COVERED)
        self.assertEqual(self.record("DOC.md > Alpha"), cr.EXIT_COVERED)
        self.assertEqual(self.quiet(["sweep"]), cr.EXIT_COVERED)

    def test_a_renamed_section_is_reported_as_a_pass_with_no_unit(self) -> None:
        """A rename leaves the old key holding a pass with no unit. The new key joins the backlog,
        so the slice picks it up, and the orphan is reported without being counted as work."""
        self.cover_all()
        self.write("DOC.md", "intro\n\n## Renamed\n\na body\n\n## Beta\n\nb body\n")
        code, output = self.loud(["sweep"])
        self.assertEqual(code, cr.EXIT_NOT_COVERED)
        self.assertIn("1 pass(es) with no unit", output)
        self.assertIn("DOC.md > Alpha", output)
        self.assertIn("DOC.md > Renamed=", output, "the renamed section is read under its new key")

    def test_a_tree_holding_every_recorded_unit_reports_no_orphan(self) -> None:
        """The heading is absent rather than reading zero, so a reader never scans a list that is
        always there."""
        self.record("DOC.md > Alpha")
        _, output = self.loud(["sweep"])
        self.assertNotIn("pass(es) with no unit", output)

    def test_the_printed_record_command_pastes_as_one_line(self) -> None:
        """The issue body invites copying it, and a wrapped line with no continuation runs `record`
        with no `--unit` at all."""
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Alpha\n\na body, edited\n\n## Beta\n\nb body\n")
        _, output = self.loud(["sweep"])
        line = (
            "python3 scripts/canonical_review.py record --reviewer agent-skill"
            " --unit '<key>=<digest>'"
        )
        self.assertIn(line, output.splitlines(), "the printed command must be one whole line")

    def test_record_refuses_an_unresolvable_target(self) -> None:
        """`record` resolves a merge-base against `--target` to stamp `hubCommit`, so an
        unresolvable target refuses the record rather than falling back to `HEAD`."""
        code, output = self.loud(
            [
                "record",
                "--reviewer",
                "agent-skill",
                "--target",
                "no-such-branch",
                "--unit",
                f"DOC.md > Alpha={self.units()['DOC.md > Alpha']}",
            ]
        )
        self.assertEqual(code, cr.EXIT_CANNOT_RUN)
        self.assertIn("no-such-branch", output)
        self.assertFalse((self.tmp / cr.LEDGER).exists(), "a refused record still wrote a ledger")

    def test_record_refuses_an_empty_target_value(self) -> None:
        """A caller written as `--target "$VAR"` with the variable unset must not stamp a base
        nobody named."""
        code = self.quiet(
            [
                "record",
                "--reviewer",
                "agent-skill",
                "--target",
                "",
                "--unit",
                f"DOC.md > Alpha={self.units()['DOC.md > Alpha']}",
            ]
        )
        self.assertEqual(code, cr.EXIT_CANNOT_RUN)

    def test_a_manifest_path_that_escapes_the_root_cannot_run(self) -> None:
        """Held to what a carried tree's own paths already are, rather than read straight off the manifest."""
        spec = json.loads((self.tmp / "spec/files.json").read_bytes().decode("utf-8"))
        spec["baseline"].append({"path": "../outside.md", "fidelity": "verbatim"})
        self.write("spec/files.json", json.dumps(spec, indent=2) + "\n")
        with self.assertRaises(cr.CannotRun):
            cr.units(self.tmp)


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

    def test_a_sweep_reports_an_unreadable_ledger_rather_than_a_verdict(self) -> None:
        self.write(cr.LEDGER, "{not json")
        self.assertEqual(self.quiet(["sweep"]), cr.EXIT_CANNOT_RUN)

    def test_recording_twice_keeps_one_entry_per_unit(self) -> None:
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited again\n\n## Beta\n\nb body\n")
        self.record("DOC.md > Alpha")
        entries = cr.read_ledger(self.tmp)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries["DOC.md > Alpha"]["digest"], self.units()["DOC.md > Alpha"])

    def test_a_recorded_pass_names_a_hub_commit(self) -> None:
        """The fixture's `task` branch has made no commit of its own yet, so its merge-base against
        `origin/develop` and its own `HEAD` are the same commit here. That coincidence is why this
        much still holds against `HEAD`. The case where they diverge, and only the merge-base is
        what gets stamped, is `test_a_recorded_pass_names_the_merge_base_not_the_branch_tip` below.
        """
        self.record("DOC.md > Alpha")
        entry = cr.read_ledger(self.tmp)["DOC.md > Alpha"]
        self.assertEqual(entry["hubCommit"], run(self.tmp, "rev-parse", "HEAD").strip())
        self.assertEqual(entry["reviewer"], "agent-skill")

    def test_a_recorded_pass_names_the_merge_base_not_the_branch_tip(self) -> None:
        """A branch's own tip is squashed away on merge and stops resolving. The merge-base against
        the target is a commit the target already holds, so it survives the squash. #1222, #1210."""
        self.write("DOC.md", "intro\n\n## Alpha\n\nedited\n\n## Beta\n\nb body\n")
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", "advance the branch tip past the merge base")
        base = run(self.tmp, "rev-parse", "refs/remotes/origin/develop").strip()
        tip = run(self.tmp, "rev-parse", "HEAD").strip()
        self.assertNotEqual(base, tip, "the fixture needs a tip that has moved past the base")
        self.record("DOC.md > Alpha")
        entry = cr.read_ledger(self.tmp)["DOC.md > Alpha"]
        self.assertEqual(entry["hubCommit"], base)
        self.assertNotEqual(entry["hubCommit"], tip)

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

    def test_two_records_in_either_order_produce_one_ledger(self) -> None:
        """The ledger is the state two branches merge, so the order two passes were recorded in
        has to leave no trace in it beyond the stamps, or the same two passes on two branches
        would be two different files."""
        alpha, beta = "DOC.md > Alpha", "DOC.md > Beta"

        def payload() -> dict[str, Any]:
            data = json.loads((self.tmp / cr.LEDGER).read_bytes().decode("utf-8"))
            for entry in data["passes"]:
                entry.pop("stamp")
            return data

        self.record(alpha)
        self.record(beta)
        first = payload()
        (self.tmp / cr.LEDGER).unlink()
        self.record(beta)
        self.record(alpha)
        self.assertEqual(first, payload())
        self.assertEqual([entry["unit"] for entry in first["passes"]], [alpha, beta])

    def test_two_branches_recording_different_units_merge_without_conflict(self) -> None:
        """Each branch records a pass over a different unit, and git merges the two ledgers
        without a conflict."""
        ledger = cr.LEDGER
        # Entries that sort between the two, since git merges two insertions only where unchanged lines separate them.
        self.record("CONF.json")
        self.record("DOC.md > Alpha")
        run(self.tmp, "add", ledger)
        run(self.tmp, "commit", "-m", "seed")
        run(self.tmp, "branch", "lane-b")
        self.record(f"{cr.AUTHORED_SKILLS}/demo/SKILL.md > Use It")
        run(self.tmp, "commit", "-am", "lane a")
        run(self.tmp, "checkout", "lane-b")
        self.record("SECT.md > Carried")
        run(self.tmp, "commit", "-am", "lane b")
        run(self.tmp, "merge", "--no-edit", "task")
        self.assertEqual(run(self.tmp, "ls-files", "--unmerged"), "")
        self.assertEqual(len(cr.read_ledger(self.tmp)), 4)

    def test_a_held_lock_refuses_the_record_rather_than_writing_past_it(self) -> None:
        """Two overlapping records would each read a ledger without the other's pass and the
        second write would drop one, so a record that cannot take the lock records nothing."""
        lock = Path(str(cr.ledger_lock(self.tmp)) + ".lock")
        lock.write_bytes(b"")
        self.addCleanup(lock.unlink, missing_ok=True)
        with unittest.mock.patch.object(cr, "LOCK_TIMEOUT", 0.2):
            self.assertEqual(self.record("DOC.md > Alpha"), cr.EXIT_CANNOT_RUN)
        self.assertEqual(cr.read_ledger(self.tmp), {})
        lock.unlink()
        self.assertEqual(self.record("DOC.md > Alpha"), cr.EXIT_COVERED)
        self.assertFalse(lock.exists(), "the record did not release its lock")

    def test_the_lock_lives_in_the_git_directory_rather_than_the_tree(self) -> None:
        """A lock beside the ledger would be untracked content in `reports/` after a crash,
        which a blanket add then commits."""
        git_dir = Path(run(self.tmp, "rev-parse", "--absolute-git-dir").strip())
        self.assertEqual(cr.ledger_lock(self.tmp).parent, git_dir)

    def test_an_orphaned_pass_is_reported_rather_than_dropped(self) -> None:
        """Deciding a section moved rather than vanished is a reader's call, not this tool's."""
        self.record("DOC.md > Alpha")
        self.write("DOC.md", "intro\n\n## Renamed\n\na body\n\n## Beta\n\nb body\n")
        _, output = self.loud(["status"])
        self.assertIn("DOC.md > Alpha", json.loads(output)["orphanedPasses"])


class BoundaryCase(RepoCase):
    """A crash is the check not having run, and must never read as the not-covered verdict."""

    def test_an_unexpected_failure_reports_the_boundary_rather_than_a_verdict(self) -> None:
        """Exit 1 is what a caller folds as "a unit moved past its pass", so a crash that fell
        through to the interpreter's own exit 1 would report an execution boundary as a finding.
        `units` stands in for any of them: it reads git and the tree on every subcommand."""
        for boom in (OSError("no fd"), subprocess.TimeoutExpired("git", 1), RuntimeError("x")):
            with unittest.mock.patch.object(cr, "units", side_effect=boom):
                code, output = self.loud(["sweep"])
            self.assertEqual(code, cr.EXIT_CANNOT_RUN, f"{type(boom).__name__} read as a verdict")
            self.assertIn("unexpected failure", output)


class CarriedPathCase(RepoCase):
    def test_a_symlinked_carried_path_is_refused(self) -> None:
        """carry.py refuses a symlink anywhere in a carried tree, and reading one here would follow
        it out of the repository the manifest describes."""
        target = self.outside / "elsewhere.md"
        target.write_bytes(b"## Alpha\n\nnot ours\n")
        (self.tmp / "DOC.md").unlink()
        try:
            (self.tmp / "DOC.md").symlink_to(target)
        except OSError as unprivileged:
            # Windows refuses a symlink without the privilege or Developer Mode.
            # That is an execution boundary rather than this guard failing.
            (self.tmp / "DOC.md").write_bytes(b"## Alpha\n\nown\n")
            raise unittest.SkipTest(f"this host cannot create a symlink: {unprivileged}") from None
        with self.assertRaises(cr.CannotRun) as caught:
            cr.units(self.tmp)
        self.assertIn("DOC.md", str(caught.exception))
        # And one pointing inside the repository, which containment alone would let through.
        (self.tmp / "DOC.md").unlink()
        (self.tmp / "DOC.md").symlink_to(self.tmp / "OWN.md")
        with self.assertRaises(cr.CannotRun) as caught:
            cr.units(self.tmp)
        self.assertIn("symlink", str(caught.exception))


class ReportCase(RepoCase):
    def test_a_record_writes_the_ledger_and_nothing_else_into_the_tree(self) -> None:
        """The burn-down carried global counts, so two branches each recording one pass merged
        silently to a count one short of the ledger's. The ledger is the only tracked state."""
        self.record("DOC.md > Alpha")
        code, text = self.loud(["report"])
        self.assertEqual(code, cr.EXIT_COVERED)
        self.assertIn("- covered: 1", text)
        untracked = run(self.tmp, "status", "--porcelain", "--untracked-files=all").splitlines()
        self.assertEqual(untracked, [f"?? {cr.LEDGER}"])

    def test_the_report_describes_the_tree_it_is_rendered_from(self) -> None:
        """A deleted unit changes no recorded pass, and a rendering made after the deletion no
        longer counts the unit that is gone."""
        _, before = self.loud(["report"])
        self.assertIn("**Beta** -", before)
        self.write("DOC.md", "intro\n\n## Alpha\n\na body\n")
        self.assertEqual(cr.read_ledger(self.tmp), {}, "the premise moved: a pass was recorded")
        _, after = self.loud(["report"])
        self.assertNotIn("**Beta** -", after)

    def test_the_report_names_every_outstanding_unit(self) -> None:
        code, text = self.loud(["report"])
        self.assertEqual(code, cr.EXIT_COVERED)
        for unit in self.units():
            section = unit.split(cr.SECTION_DELIM, 1)[-1] if cr.SECTION_DELIM in unit else unit
            self.assertIn(section, text, f"{unit} is missing from the burn-down")

    def test_the_report_counts_a_recorded_pass(self) -> None:
        self.record("DOC.md > Alpha")
        _, text = self.loud(["report"])
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
