#!/usr/bin/env python3
"""Exercise carried-link portability checks against a crafted manifest."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "spec"))
import validate


class CarriedRelativeLinkCase(unittest.TestCase):
    """A hub-valid relative link must also resolve after its section is carried."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.baseline = [
            {
                "path": "GOVERNANCE.md",
                "appliesTo": "*",
                "sections": [{"name": "Rule", "fidelity": "verbatim"}],
            },
            {"path": "WORKFLOW.md", "appliesTo": "*"},
        ]

    def write_governance(self, target: str) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            f"# Governance\n\n## Rule\n\nRead [the contract]({target}).\n",
            encoding="utf-8",
        )

    def test_rejects_a_hub_only_relative_target(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "hub-only.md").write_text("# Hub only\n", encoding="utf-8")
        self.write_governance("./docs/hub-only.md")

        self.assertEqual(
            validate.carried_link_errors(self.root, self.baseline),
            [
                (
                    "files.json: GOVERNANCE.md section 'Rule' links to relative target "
                    "'./docs/hub-only.md', which is not universally carried"
                )
            ],
        )

    def test_accepts_a_universally_carried_relative_target(self) -> None:
        self.write_governance("./WORKFLOW.md#contract")

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_rejects_an_absolute_template_repository_target(self) -> None:
        self.write_governance(
            "https://github.com/ptr727/ProjectTemplate/blob/main/.github/workflows/publish-release.yml"
        )

        self.assertEqual(
            validate.carried_link_errors(self.root, self.baseline),
            [
                (
                    "files.json: GOVERNANCE.md section 'Rule' links to the template repository "
                    "at 'https://github.com/ptr727/ProjectTemplate/blob/main/.github/workflows/"
                    "publish-release.yml'"
                )
            ],
        )

    def test_rejects_the_template_repository_root_with_a_fragment(self) -> None:
        self.write_governance("https://github.com/ptr727/ProjectTemplate#readme")

        self.assertEqual(1, len(validate.carried_link_errors(self.root, self.baseline)))

    def test_ignores_markdown_syntax_inside_inline_code(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\nStrip the `[text](url)` syntax.\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_ignores_relative_links_inside_fenced_code(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\n~~~markdown\n[hub only](./docs/hub-only.md)\n~~~\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_accepts_a_universally_carried_reference_target(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\nRead [the contract][contract].\n\n"
            "[contract]: ./WORKFLOW.md#contract\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_accepts_a_different_repository_with_the_template_name_as_a_prefix(self) -> None:
        (self.root / "GOVERNANCE.md").write_text(
            "# Governance\n\n## Rule\n\n"
            "Read [the related repository](https://github.com/ptr727/ProjectTemplate-fork).\n",
            encoding="utf-8",
        )

        self.assertEqual(validate.carried_link_errors(self.root, self.baseline), [])

    def test_rejects_a_hub_only_target_in_a_whole_intent_file(self) -> None:
        (self.root / "AUDIT.md").write_text(
            "# Audit\n\nRead [the registry](./registry/repos.json).\n",
            encoding="utf-8",
        )
        baseline = [{"path": "AUDIT.md", "fidelity": "intent", "appliesTo": "*"}]

        self.assertEqual(
            validate.carried_link_errors(self.root, baseline),
            [
                (
                    "files.json: AUDIT.md whole file links to relative target "
                    "'./registry/repos.json', which is not universally carried"
                )
            ],
        )

    def test_checks_an_explicit_whole_intent_file_that_also_lists_sections(self) -> None:
        (self.root / "AUDIT.md").write_text(
            "# Audit\n\nRead [the registry](./registry/repos.json).\n",
            encoding="utf-8",
        )
        baseline = [
            {
                "path": "AUDIT.md",
                "fidelity": "intent",
                "whole": True,
                "appliesTo": "*",
                "sections": [{"name": "Audit", "fidelity": "intent"}],
            }
        ]

        self.assertEqual(1, len(validate.carried_link_errors(self.root, baseline)))


class DescriptionErrorsCase(unittest.TestCase):
    """registry/repos.json's optional `description` (GOVERNANCE.md "Repository Details")."""

    def test_a_plain_short_sentence_is_clean(self) -> None:
        self.assertEqual(validate.description_errors("Fixture", "A short tagline."), [])

    def test_whitespace_only_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "   "),
            ["Fixture: description must be a non-empty string"],
        )

    def test_an_explicit_null_is_rejected_rather_than_read_as_absent(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", None),
            ["Fixture: description must be a non-empty string"],
        )

    def test_for_repo_an_absent_key_produces_no_errors(self) -> None:
        self.assertEqual(validate.description_errors_for_repo({}, "Fixture"), [])

    def test_for_repo_an_explicit_null_is_rejected_rather_than_read_as_absent(self) -> None:
        # Locks in the presence-vs-None guard: this regresses to `[]` if it is ever weakened back to `is not None`.
        self.assertEqual(
            validate.description_errors_for_repo({"description": None}, "Fixture"),
            ["Fixture: description must be a non-empty string"],
        )

    def test_a_non_string_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", 42),
            ["Fixture: description must be a non-empty string"],
        )

    def test_an_inline_markdown_link_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "See [docs](https://example.test) for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_a_reference_style_markdown_link_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "See [docs][ref] for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_a_nested_bracket_link_label_is_rejected(self) -> None:
        # Regresses a gap where `[^\]]*` stopped at the first `]` and missed a label with its own brackets.
        self.assertEqual(
            validate.description_errors(
                "Fixture", "See [API [docs]](https://example.test) for more."
            ),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_a_destination_with_two_parenthesized_groups_is_rejected(self) -> None:
        # Regresses the matching gap on the destination side: more than one balanced `()` run after the link.
        self.assertEqual(
            validate.description_errors(
                "Fixture", "See [docs](https://example.test/a_(b)_(c)) for more."
            ),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_a_link_nested_inside_a_non_link_bracket_run_is_still_rejected(self) -> None:
        # A failed outer span used to jump past the whole run instead of retrying one character in.
        # That skipped the valid inner link in `[[docs](url)]` (#1011, qodo).
        self.assertEqual(
            validate.description_errors("Fixture", "See [[docs](url)] for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_an_escaped_bracket_inside_a_label_does_not_corrupt_the_match(self) -> None:
        # A backslash-escaped `\[` used to count as real nesting, corrupting the label match.
        # It reads as a literal character instead (#1011, qodo).
        self.assertEqual(
            validate.description_errors("Fixture", r"See [API \[docs](url) for more."),
            ["Fixture: description carries Markdown links - keep it link-free plain text"],
        )

    def test_leading_or_trailing_whitespace_is_rejected(self) -> None:
        # Not silently trimmed here, even though spec/audit.py and configure.sh both strip it defensively.
        # Rejecting it at the source keeps the registry's own text the exact canonical form every mirror carries.
        self.assertEqual(
            validate.description_errors("Fixture", "  A short tagline.  "),
            [
                "Fixture: description must be plain single-line text with no leading or trailing whitespace"
            ],
        )

    def test_an_embedded_newline_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "A tagline.\nA second line."),
            [
                "Fixture: description must be plain single-line text with no leading or trailing whitespace"
            ],
        )

    def test_a_long_run_of_unmatched_brackets_stays_linear(self) -> None:
        # A run of unmatched '[' used to re-scan the remaining text from every position.
        # That was O(N^2) (#1011, CodeRabbit), and a slow run here means a regression back to it.
        start = time.monotonic()
        validate.contains_description_markdown_link("[" * 20000)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_exactly_the_cap_is_clean(self) -> None:
        self.assertEqual(validate.description_errors("Fixture", "a" * 100), [])

    def test_over_the_cap_is_rejected(self) -> None:
        self.assertEqual(
            validate.description_errors("Fixture", "a" * 101),
            ["Fixture: description is 101 characters, over the 100-char limit"],
        )


class TreeSourceRootCase(unittest.TestCase):
    """A tree source naming the repository root is refused however it is spelled."""

    def test_rejects_every_spelling_that_reduces_to_the_root(self) -> None:
        for value in (".", "./", ".//.", ".///.", "./."):
            with self.subTest(value=value):
                self.assertTrue(validate.reduces_to_repo_root(value))

    def test_accepts_a_source_below_the_root(self) -> None:
        for value in (
            ".github/skills",
            "spec",
            "a/b/c",
            ".github",
            "./docs",
            "docs//sub",
            "docs/./sub",
        ):
            with self.subTest(value=value):
                self.assertFalse(validate.reduces_to_repo_root(value))


class RegistryNameUniquenessCase(unittest.TestCase):
    """A registry name is the key both configure.sh and audit.py resolve an entry by.

    The check lives inside `main()`, so this runs the real script against a scratch tree rather
    than calling a function that does not exist to be called. The registry is a self-contained
    fixture rather than the live one plus an entry, so an unrelated registry edit cannot move
    the result and the two cases differ only in the thing under test.
    """

    def run_against(self, names: list[str]) -> str:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(validate.ROOT / "spec", root / "spec")
            (root / "registry").mkdir()
            # The marker entry carries one deliberate defect the loop always reports, which is how each case proves the loop ran.
            # Keying that proof on the names under test would leave the absence assertion below passing on a run that never reached them.
            marker = {"name": "LoopMarker", "url": "not-a-url", "status": "backlog"}
            fixture = {
                "defaults": {"workflowModel": "release"},
                "repos": [
                    {
                        "name": n,
                        "url": f"https://github.com/owner{i}/repo{i}",
                        "status": "backlog",
                        "classificationPending": True,
                    }
                    for i, n in enumerate(names)
                ]
                + [marker],
            }
            (root / "registry" / "repos.json").write_text(json.dumps(fixture), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(root / "spec" / "validate.py")],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertIn(
                "LoopMarker: url is not a github.com/<owner>/<repo> URL",
                output,
                "the registry loop was never reached, so an absence assertion would be vacuous",
            )
            return output

    def test_a_name_differing_only_by_case_is_rejected(self) -> None:
        """audit.py narrows on the same casefold, so both entries would answer one --repo."""
        self.assertIn(
            "blog: duplicate registry entry for name 'blog', already declared as 'Blog'",
            self.run_against(["Blog", "blog"]),
        )

    def test_two_names_colliding_only_under_casefold_are_rejected(self) -> None:
        """Lowercasing keeps these two distinct, so this case is what pins the dedup's normalizer."""
        sharp_s, doubled = "Stra\u00dfe", "Strasse"
        self.assertNotEqual(sharp_s.lower(), doubled.lower())
        self.assertEqual(sharp_s.casefold(), doubled.casefold())
        self.assertIn(
            "duplicate registry entry for name 'Strasse', already declared as 'Stra\u00dfe'",
            self.run_against([sharp_s, doubled]),
        )

    def test_the_collision_is_found_whichever_order_the_pair_is_declared(self) -> None:
        """Declared the other way round, the lookup rather than the insert is what has to casefold."""
        self.assertIn(
            "duplicate registry entry for name 'Stra\u00dfe', already declared as 'Strasse'",
            self.run_against(["Strasse", "Stra\u00dfe"]),
        )

    def test_a_byte_identical_name_names_the_entry_already_declared(self) -> None:
        """The commonest shape, and the one a case-differing qualifier would misdescribe."""
        self.assertIn(
            "Blog: duplicate registry entry for name 'Blog', already declared as 'Blog'",
            self.run_against(["Blog", "Blog"]),
        )

    def test_two_distinct_names_are_accepted(self) -> None:
        self.assertNotIn("duplicate registry entry for name", self.run_against(["Blog", "Utils"]))


class RegistryNameNormalizerCase(unittest.TestCase):
    """The narrowing in spec/audit.py, which must normalize a name the way spec/validate.py dedupes it.

    This reads only spec/audit.py. The dedup's own normalizer is pinned by
    `RegistryNameUniquenessCase` above, whose casefold-only collision fails under lowercasing.

    The narrowing lives inside `main()`, so the three lines that do it are lifted out of the file
    and executed here. Restating them would leave a test that agrees with itself while the two
    files disagree, which is the defect this pins.
    """

    def narrow(self, declared: list[str], typed: list[str]) -> tuple[list[str], set[str]]:
        source = (validate.ROOT / "spec" / "audit.py").read_text(encoding="utf-8")
        lines = re.findall(
            r"^(    wanted = \{.*?\n)|^(        repos = \[r for r in repos.*?\n)|^(        missing = wanted -.*?\n)",
            source,
            re.MULTILINE,
        )
        # The three sit at two indent levels in their own function, so each is dedented on its own.
        found = [part.strip() for group in lines for part in group if part]
        self.assertEqual(len(found), 3, f"expected three narrowing lines, got: {found}")
        block = "\n".join(found)
        # One dict as both globals and locals, because with two a comprehension's free variable resolves against the empty globals and raises below 3.12 rather than reading the local.
        scope: dict = {
            "a": type("Args", (), {"names": typed})(),
            "repos": [{"name": n} for n in declared],
        }
        exec(block, scope)  # noqa: S102
        selected, missing = scope["repos"], scope["missing"]
        # 7. The lifted lines are source rather than typed code, so their shapes are asserted.
        self.assertIsInstance(selected, list)
        self.assertIsInstance(missing, set)
        return [r["name"] for r in selected], missing

    def test_a_name_whose_casefold_differs_from_its_lowercase_is_selectable(self) -> None:
        """`Stra` + sharp s lowercases to itself and casefolds to `strasse`, so the two disagree."""
        sharp_s = "Stra\u00dfe"
        self.assertNotEqual(sharp_s.lower(), sharp_s.casefold())
        # The typed name carries the character too, so the line normalizing `a.names` is pinned alongside the two normalizing the entries.
        # An ASCII spelling would leave that first line free.
        typed = "STRA\u00dfE"
        self.assertNotEqual(typed.lower(), typed.casefold())
        selected, missing = self.narrow([sharp_s], [typed])
        self.assertEqual(selected, [sharp_s])
        self.assertEqual(missing, set())

    def test_an_absent_name_is_reported_missing(self) -> None:
        selected, missing = self.narrow(["Blog"], ["Utils"])
        self.assertEqual(selected, [])
        self.assertEqual(missing, {"utils"})


class RegistryEnvironmentCase(unittest.TestCase):
    """A registry `environments` entry is shape-checked here, since CI runs no JSON-schema validation."""

    def errors(self, envs) -> list[str]:
        return validate.environment_errors_for_repo({"environments": envs}, "Fixture")

    def test_a_repo_declaring_no_environments_is_clean(self) -> None:
        self.assertEqual(validate.environment_errors_for_repo({}, "Fixture"), [])

    def test_each_valid_branch_policy_form_is_clean(self) -> None:
        for env in (
            {"name": "pypi", "branchPolicy": "custom", "branches": ["develop", "main"]},
            {"name": "production", "branchPolicy": "none"},
        ):
            with self.subTest(policy=env["branchPolicy"]):
                self.assertEqual(self.errors([env]), [])

    def test_a_custom_policy_declaring_no_branches_is_rejected(self) -> None:
        self.assertEqual(
            self.errors([{"name": "pypi", "branchPolicy": "custom"}]),
            ["Fixture: environments[0] branchPolicy custom must declare 'branches'"],
        )

    def test_a_custom_policy_declaring_an_empty_branch_set_is_accepted(self) -> None:
        """Presence is the test, so an empty list declares that the environment allows nothing."""
        self.assertEqual(
            self.errors([{"name": "pypi", "branchPolicy": "custom", "branches": []}]), []
        )

    def test_a_policy_naming_no_branch_set_may_not_declare_one(self) -> None:
        self.assertEqual(
            self.errors([{"name": "e", "branchPolicy": "none", "branches": ["main"]}]),
            [
                (
                    "Fixture: environments[0] branchPolicy none names no branch set, "
                    "so it must not declare 'branches'"
                )
            ],
        )

    def test_githubs_protected_branches_form_is_not_declarable(self) -> None:
        """It counts classic branch protection, which the fleet removes, so declaring it would assert a gate that does not exist."""
        self.assertEqual(
            self.errors([{"name": "e", "branchPolicy": "protected"}]),
            [("Fixture: environments[0] branchPolicy 'protected' invalid (expected custom, none)")],
        )

    def test_an_unknown_branch_policy_is_rejected(self) -> None:
        self.assertEqual(
            self.errors([{"name": "e", "branchPolicy": "everything"}]),
            [
                (
                    "Fixture: environments[0] branchPolicy 'everything' invalid "
                    "(expected custom, none)"
                )
            ],
        )

    def test_a_missing_branch_policy_is_reported_as_absent(self) -> None:
        """Absence goes through its own branch: the repr of a missing field differs from the valid "none" by case alone."""
        self.assertEqual(
            self.errors([{"name": "e"}]),
            ["Fixture: environments[0] missing 'branchPolicy' (expected custom, none)"],
        )

    def test_an_explicit_null_is_declared_but_invalid_not_absent(self) -> None:
        """Presence is the test, matching description_errors_for_repo, so a null cannot pass as a repo declaring none."""
        self.assertEqual(
            validate.environment_errors_for_repo({"environments": None}, "Fixture"),
            ["Fixture: environments must be a list"],
        )

    def test_a_missing_or_empty_name_is_rejected(self) -> None:
        for env in ({"branchPolicy": "none"}, {"name": "  ", "branchPolicy": "none"}):
            with self.subTest(env=env):
                self.assertEqual(
                    self.errors([env]),
                    ["Fixture: environments[0] missing or empty 'name'"],
                )

    def test_a_name_with_surrounding_whitespace_is_rejected(self) -> None:
        """Padding survives the empty check, since a padded name strips to something."""
        for name in ("pypi ", " pypi", "\tpypi"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.errors([{"name": name, "branchPolicy": "none"}]),
                    [f"Fixture: environments[0] name {name!r} has leading or trailing whitespace"],
                )

    def test_a_padded_name_beside_its_trimmed_twin_is_reported_for_its_padding(self) -> None:
        """The two are different strings, so they never collided as duplicates, before this check or after it."""
        self.assertEqual(
            self.errors(
                [
                    {"name": "pypi", "branchPolicy": "none"},
                    {"name": "pypi ", "branchPolicy": "none"},
                ]
            ),
            ["Fixture: environments[1] name 'pypi ' has leading or trailing whitespace"],
        )

    def test_identical_padded_names_report_the_padding_and_defer_the_duplicate(self) -> None:
        """A padded name never reaches the seen set, so the duplicate it also is surfaces on the run after the padding is fixed.

        Reporting the padding alone is the deliberate half: the duplicate is a consequence of it
        rather than a second defect, and naming both would report a symptom beside its cause.
        """
        self.assertEqual(
            self.errors(
                [
                    {"name": "pypi ", "branchPolicy": "none"},
                    {"name": "pypi ", "branchPolicy": "none"},
                ]
            ),
            [
                "Fixture: environments[0] name 'pypi ' has leading or trailing whitespace",
                "Fixture: environments[1] name 'pypi ' has leading or trailing whitespace",
            ],
        )

    def test_two_entries_for_one_environment_are_rejected(self) -> None:
        self.assertEqual(
            self.errors(
                [{"name": "pypi", "branchPolicy": "none"}, {"name": "pypi", "branchPolicy": "none"}]
            ),
            ["Fixture: environments[1] duplicate environment 'pypi'"],
        )

    def test_a_non_list_and_a_non_object_entry_are_rejected(self) -> None:
        self.assertEqual(self.errors({"name": "e"}), ["Fixture: environments must be a list"])
        self.assertEqual(self.errors(["pypi"]), ["Fixture: environments[0] must be an object"])

    def test_branches_must_be_non_empty_strings(self) -> None:
        for branches in (["main", ""], [1], "main"):
            with self.subTest(branches=branches):
                self.assertEqual(
                    self.errors([{"name": "e", "branchPolicy": "custom", "branches": branches}]),
                    ["Fixture: environments[0] branches must be a list of non-empty strings"],
                )

    def test_branches_with_surrounding_whitespace_are_rejected(self) -> None:
        """Every padded entry is named, since reporting only the first would hide the rest behind one fix round."""
        self.assertEqual(
            self.errors(
                [{"name": "e", "branchPolicy": "custom", "branches": [" main", "dev", "next\t"]}]
            ),
            [
                "Fixture: environments[0] branches [' main', 'next\\t'] have leading or trailing whitespace"
            ],
        )

    def test_an_empty_branch_is_reported_as_empty_rather_than_as_padded(self) -> None:
        """A whitespace-only branch strips to nothing, so the non-empty check owns it and reports it once."""
        self.assertEqual(
            self.errors([{"name": "e", "branchPolicy": "custom", "branches": ["main", "  "]}]),
            ["Fixture: environments[0] branches must be a list of non-empty strings"],
        )

    def test_the_live_registry_declares_only_shapes_this_check_accepts(self) -> None:
        """The fixtures above prove the rule, and this proves the registry the rule is applied to."""
        registry = json.loads(
            (validate.ROOT / "registry" / "repos.json").read_text(encoding="utf-8")
        )
        for repo in registry["repos"]:
            with self.subTest(repo=repo.get("name")):
                self.assertEqual(validate.environment_errors_for_repo(repo, repo["name"]), [])


if __name__ == "__main__":
    unittest.main()
