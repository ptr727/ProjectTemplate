#!/usr/bin/env python3
"""Exercise carried-link portability checks against a crafted manifest."""

from __future__ import annotations

import ast
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
            # The gate reads the schema's own property names to check for an unknown key, so the scratch tree carries it.
            shutil.copy(
                validate.ROOT / "registry" / "repos.schema.json",
                root / "registry" / "repos.schema.json",
            )
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

    SHAPE = "Fixture: environments[0] name {!r} is not one line of printable ASCII with no leading or trailing space"

    def test_a_missing_or_empty_name_is_rejected(self) -> None:
        for env in ({"branchPolicy": "none"}, {"name": "", "branchPolicy": "none"}):
            with self.subTest(env=env):
                self.assertEqual(
                    self.errors([env]),
                    ["Fixture: environments[0] missing or empty 'name'"],
                )

    def test_a_blank_name_is_reported_by_the_grammar_rather_than_as_empty(self) -> None:
        """It is a non-empty string of the wrong shape, and calling it empty needs the notion of whitespace the grammar drops."""
        self.assertEqual(
            self.errors([{"name": "  ", "branchPolicy": "none"}]),
            [self.SHAPE.format("  ")],
        )

    def test_a_name_with_surrounding_whitespace_is_rejected(self) -> None:
        """Padding survives an emptiness check, since a padded name is a non-empty string."""
        for name in ("pypi ", " pypi", "\tpypi"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.errors([{"name": name, "branchPolicy": "none"}]),
                    [self.SHAPE.format(name)],
                )

    def test_a_name_padded_with_something_str_strip_does_not_remove_is_rejected(self) -> None:
        """The defect a whitespace test cannot reach: each of these is invisible, is left standing by str.strip(),
        and is exactly as unmatchable by configure.sh's `select(.name == $n)` as a trailing space.
        """
        for pad in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u180e"):
            name = f"pypi{pad}"
            with self.subTest(pad=pad):
                self.assertEqual(name, name.strip())
                self.assertEqual(
                    self.errors([{"name": name, "branchPolicy": "none"}]),
                    [self.SHAPE.format(name)],
                )

    def test_a_name_carrying_an_interior_space_is_accepted(self) -> None:
        """GitHub documents no character restriction on the name beyond length and uniqueness, and nothing downstream splits on one."""
        self.assertEqual(self.errors([{"name": "prod env", "branchPolicy": "none"}]), [])

    def test_a_padded_name_beside_its_trimmed_twin_is_reported_for_its_padding(self) -> None:
        """The two are different strings, so they never collided as duplicates, before this check or after it."""
        self.assertEqual(
            self.errors(
                [
                    {"name": "pypi", "branchPolicy": "none"},
                    {"name": "pypi ", "branchPolicy": "none"},
                ]
            ),
            [self.SHAPE.format("pypi ").replace("environments[0]", "environments[1]")],
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
                self.SHAPE.format("pypi "),
                self.SHAPE.format("pypi ").replace("environments[0]", "environments[1]"),
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

    def test_branches_off_the_grammar_are_rejected(self) -> None:
        """Every offending entry is named, since reporting only the first would hide the rest behind one fix round."""
        self.assertEqual(
            self.errors(
                [
                    {
                        "name": "e",
                        "branchPolicy": "custom",
                        "branches": [" main", "dev", "next\t", "  ", "rel\u200b"],
                    }
                ]
            ),
            [
                "Fixture: environments[0] branches [' main', 'next\\t', '  ', 'rel\\u200b']"
                + " are not printable ASCII with no spaces"
            ],
        )

    def test_a_ref_pattern_carrying_a_slash_and_a_star_is_accepted(self) -> None:
        """A deployment branch policy name is a pattern, so a grammar fitted to the three plain names the registry
        declares today would pass every test here and refuse the first adopter declaring a release line.
        """
        self.assertEqual(
            self.errors(
                [
                    {
                        "name": "e",
                        "branchPolicy": "custom",
                        "branches": ["releases/*", "v1.*", "feature/JIRA-123"],
                    }
                ]
            ),
            [],
        )

    def test_a_branch_declared_twice_is_rejected(self) -> None:
        """configure.sh sorts and joins both sides, so the duplicate makes the declaration longer than any live set."""
        self.assertEqual(
            self.errors(
                [{"name": "e", "branchPolicy": "custom", "branches": ["main", "main", "dev"]}]
            ),
            ["Fixture: environments[0] branches ['main'] are declared more than once"],
        )

    def test_an_empty_branch_is_reported_as_empty_rather_than_off_the_grammar(self) -> None:
        """The empty string carries no shape to report, so the non-empty check owns it and reports it once."""
        self.assertEqual(
            self.errors([{"name": "e", "branchPolicy": "custom", "branches": ["main", ""]}]),
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


class RegistryEntryGateCase(unittest.TestCase):
    """The checks that live inside `main()`, run as the real script against a scratch tree.

    Calling the helper functions directly proves the grammar and leaves the wiring unproven: deleting the
    `errors.extend(...)` line, or putting a `.strip()` back before the url match, leaves every direct-call test green.
    The registry is a self-contained fixture rather than the live one plus an entry, so an unrelated registry edit
    cannot move the result.
    """

    def run_against(self, entry: dict, defaults: dict | None = None) -> str:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(validate.ROOT / "spec", root / "spec")
            (root / "registry").mkdir()
            # The gate reads the schema's own property names to check for an unknown key, so the scratch tree carries it.
            shutil.copy(
                validate.ROOT / "registry" / "repos.schema.json",
                root / "registry" / "repos.schema.json",
            )
            # One deliberate defect the loop always reports, which is how each case proves the loop ran at all.
            marker = {"name": "LoopMarker", "url": "not-a-url", "status": "backlog"}
            base = {"workflowModel": "release"}
            base.update(defaults or {})
            fixture = {"defaults": base, "repos": [entry, marker]}
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

    def entry(self, **overrides: object) -> dict:
        base = {
            "name": "Fixture",
            "url": "https://github.com/owner/Fixture",
            "status": "backlog",
            "classificationPending": True,
        }
        base.update(overrides)
        # An override of None drops the key, which is how a case declares a field absent rather than declared null.
        return {k: v for k, v in base.items() if v is not None}

    def test_a_declared_ground_truth_branch_is_checked_by_the_loop(self) -> None:
        self.assertIn(
            "Fixture: groundTruthBranch 'main?ref=x' does not address unencoded",
            self.run_against(self.entry(groundTruthBranch="main?ref=x")),
        )

    def test_a_valid_ground_truth_branch_passes_the_loop(self) -> None:
        output = self.run_against(self.entry(groundTruthBranch="release/2.0"))
        self.assertNotIn("groundTruthBranch", output)

    def test_the_defaults_key_is_checked_by_the_same_grammar(self) -> None:
        """registry/repos.schema.json patterns this key, so a gate that skipped it would be the looser of the two."""
        self.assertIn(
            "defaults: groundTruthBranch 'a/../../../../../zen' does not address unencoded",
            self.run_against(self.entry(), defaults={"groundTruthBranch": "a/../../../../../zen"}),
        )

    def test_a_valid_defaults_key_passes(self) -> None:
        output = self.run_against(self.entry(), defaults={"groundTruthBranch": "develop"})
        self.assertNotIn("groundTruthBranch", output)

    def test_a_padded_url_is_refused_rather_than_trimmed(self) -> None:
        """The `.strip()` this replaced accepted a value spec/audit.py then addressed with the padding still on."""
        for url in (
            "https://github.com/owner/Fixture ",
            " https://github.com/owner/Fixture",
            "https://github.com/owner/Fixture\n",
        ):
            with self.subTest(url=url):
                self.assertIn(
                    "Fixture: url is not a github.com/<owner>/<repo> URL",
                    self.run_against(self.entry(url=url)),
                )

    def test_a_git_suffixed_url_passes_the_loop(self) -> None:
        """The url is legitimate and the gate always accepted it, so the defect was the slug it produced.

        AuditRepoSlugCase below is what pins the slug; this pins that the gate did not start refusing the url.
        """
        output = self.run_against(self.entry(url="https://github.com/owner/Fixture.git"))
        self.assertNotIn("Fixture: url is not", output)

    def test_a_name_disagreeing_with_its_url_is_refused(self) -> None:
        """repo-config/configure.sh keys the registry on the url's segment, so the entry resolves to nothing there."""
        self.assertIn(
            "Other: name and url disagree, the url naming repo 'Fixture'",
            self.run_against(self.entry(name="Other")),
        )

    def test_agreement_is_measured_after_the_git_suffix_is_stripped(self) -> None:
        """The identity drops a trailing `.git`, so a name must equal what the audit addresses rather than the raw tail."""
        output = self.run_against(self.entry(url="https://github.com/owner/Fixture.git"))
        self.assertNotIn("name and url disagree", output)

    def test_agreement_is_the_names_own_grammar(self) -> None:
        """A name carrying an invisible character is refused with no pattern of its own, since no url segment holds one.

        This is the case a `.strip()` truthiness test admitted: the value is already trimmed, so it passed both name
        checks and then matched no entry in configure.sh or spec/audit.py.
        """
        zero_width = "Fixture\u200b"
        self.assertEqual(zero_width.strip(), zero_width)
        self.assertIn(
            "name and url disagree, the url naming repo 'Fixture'",
            self.run_against(self.entry(name=zero_width)),
        )

    def test_a_non_boolean_has_develop_is_refused(self) -> None:
        """spec/audit.py coerces it, so "no" reads as true and [] reads as false while the DRIFT line prints the raw value."""
        coerced: list[object] = ["no", [], 1]
        for declared in coerced:
            with self.subTest(declared=declared):
                self.assertIn(
                    "Fixture: hasDevelop",
                    self.run_against(self.entry(hasDevelop=declared)),
                )

    def test_a_declared_false_has_develop_passes(self) -> None:
        """Presence is the test, so the answer for a repo with no develop branch is not read as an undeclared field."""
        self.assertNotIn("hasDevelop", self.run_against(self.entry(hasDevelop=False)))

    def test_a_cataloged_repo_declaring_false_satisfies_the_requirement(self) -> None:
        """The requirement is presence, and a truthiness test would report this legitimate declaration as absent.

        The case above runs on a backlog fixture, where the cataloged-only requirement cannot fire at all, so it
        proves the type check and nothing about the requirement. This one is what the truthiness mutant fails.
        """
        output = self.run_against(
            self.entry(
                status="cataloged",
                types=["source-only"],
                consumerModel="pull",
                hasDevelop=False,
                classificationPending=None,
            )
        )
        self.assertNotIn("must declare hasDevelop", output)

    def test_a_cataloged_repo_must_declare_has_develop(self) -> None:
        """Absent, it is coerced to false and audited against the live branch, so omitting it asserts rather than declines."""
        self.assertIn(
            "Fixture: cataloged repo must declare hasDevelop",
            self.run_against(
                self.entry(
                    status="cataloged",
                    types=["source-only"],
                    consumerModel="pull",
                    classificationPending=None,
                )
            ),
        )

    def test_a_required_secret_that_is_not_a_github_secret_name_is_refused(self) -> None:
        """A padded name is reported missing from the actions store on every run, and the unpadded one goes unrequired."""
        for declared in (" CODECOV_TOKEN", "CODECOV_TOKEN ", "CODECOV TOKEN", "1TOKEN", "", 7):
            with self.subTest(declared=declared):
                self.assertIn(
                    "Fixture: requiredSecrets entry",
                    self.run_against(self.entry(requiredSecrets=[declared])),
                )

    def test_the_secret_names_the_registry_declares_pass(self) -> None:
        output = self.run_against(
            self.entry(requiredSecrets=["CODECOV_TOKEN", "NUGET_USERNAME", "_LEADING"])
        )
        self.assertNotIn("requiredSecrets entry", output)

    def test_a_non_list_required_secrets_is_refused(self) -> None:
        """A bare string would otherwise become a set of its characters, matching no secret name there is."""
        self.assertIn(
            "Fixture: requiredSecrets must be an array of secret names",
            self.run_against(self.entry(requiredSecrets="CODECOV_TOKEN")),
        )

    def test_a_declared_null_list_is_a_wrong_type_rather_than_an_absent_field(self) -> None:
        """`entry.get(key, [])` returns the null rather than the default, so spec/audit.py iterates it and raises.

        The entry helper drops a None value, which is how a case declares a field absent, so both nulls are placed
        directly here. A guard written as `is not None` skips its own check on exactly this shape.
        """
        for key, message in (
            ("requiredSecrets", "requiredSecrets must be an array of secret names"),
            ("driftNotes", "driftNotes must be an array of notes"),
        ):
            with self.subTest(key=key):
                entry = self.entry()
                entry[key] = None
                self.assertIn(f"Fixture: {message}", self.run_against(entry))

    def test_a_malformed_required_secrets_on_a_cataloged_repo_reports_rather_than_raises(
        self,
    ) -> None:
        """The cross-check below the loop builds a set from the same value, and every error prints after the loop.

        An unguarded set() there raises on the malformed value, so the operator gets a traceback in place of the
        message this check appended, which is the one finding that would have named the defect.
        """
        output = self.run_against(
            self.entry(
                status="cataloged",
                types=["source-only"],
                consumerModel="pull",
                hasDevelop=True,
                requiredSecrets=7,
                classificationPending=None,
            )
        )
        self.assertNotIn("Traceback", output)
        self.assertIn("Fixture: requiredSecrets must be an array of secret names", output)

    def test_an_unhashable_required_secret_reports_rather_than_raises(self) -> None:
        """A container-type guard passes a nested list, which then raises on the way into the cross-check's set().

        The non-list case above is a different shape and does not reach this line at all, so it proves nothing
        about it. `set(["a", ["b"]])` raises TypeError: unhashable type: 'list'.
        """
        unhashable: list[list[object]] = [[["CODECOV_TOKEN"]], [{}], [[]]]
        for declared in unhashable:
            with self.subTest(declared=declared):
                output = self.run_against(
                    self.entry(
                        status="cataloged",
                        types=["source-only"],
                        consumerModel="pull",
                        hasDevelop=True,
                        requiredSecrets=declared,
                        classificationPending=None,
                    )
                )
                self.assertNotIn("Traceback", output)
                self.assertIn("Fixture: requiredSecrets entry", output)

    def test_a_schema_that_is_not_valid_utf8_is_reported_rather_than_raised(self) -> None:
        """read_text raises UnicodeDecodeError, a ValueError that is not a JSONDecodeError, so naming that
        subclass alone let it escape the guard and exit on a traceback.
        """
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(validate.ROOT / "spec", root / "spec")
            (root / "registry").mkdir()
            (root / "registry" / "repos.json").write_text(
                json.dumps({"defaults": {}, "repos": [self.entry()]}), encoding="utf-8"
            )
            (root / "registry" / "repos.schema.json").write_bytes(b'{"a": "\x80"}')
            result = subprocess.run(
                [sys.executable, str(root / "spec" / "validate.py")],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertNotIn("Traceback", output)
            self.assertIn("repos.schema.json: cannot read the declared property names", output)

    def test_an_unreadable_schema_is_reported_rather_than_raised(self) -> None:
        """The unknown-key check reads the schema, and skipping it silently reads exactly like a clean registry."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            shutil.copytree(validate.ROOT / "spec", root / "spec")
            (root / "registry").mkdir()
            (root / "registry" / "repos.json").write_text(
                json.dumps({"defaults": {}, "repos": [self.entry()]}), encoding="utf-8"
            )
            # No repos.schema.json at all, which raises OSError rather than reaching the wrong-shape case.
            result = subprocess.run(
                [sys.executable, str(root / "spec" / "validate.py")],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output = result.stdout + result.stderr
            self.assertNotIn("Traceback", output)
            self.assertIn("repos.schema.json: cannot read the declared property names", output)

    def test_a_drift_note_that_is_not_a_note_is_refused(self) -> None:
        """spec/audit.py slices each note and runs a regex over it, so a non-string raises mid-run rather than reporting."""
        for declared in (None, 7, "", "   "):
            with self.subTest(declared=declared):
                self.assertIn(
                    "Fixture: driftNotes entry",
                    self.run_against(self.entry(driftNotes=[declared])),
                )

    def test_a_real_drift_note_passes(self) -> None:
        output = self.run_against(self.entry(driftNotes=["Governance hub; audits its own rules."]))
        self.assertNotIn("driftNotes entry", output)

    def test_an_unknown_key_on_a_repo_entry_is_refused(self) -> None:
        """The schema marks the entry additionalProperties: false and no gate runs the schema, so this is the only reader.

        A misspelled key validates clean otherwise, and the audit reads its own default without ever saying that the
        declaration it was handed went unread.
        """
        self.assertIn(
            "Fixture: unknown key(s) groundTruthBanch",
            self.run_against(self.entry(groundTruthBanch="develop")),
        )

    def test_an_unknown_key_on_the_defaults_object_is_refused(self) -> None:
        self.assertIn(
            "defaults: unknown key(s) groundTruthBanch",
            self.run_against(self.entry(), defaults={"groundTruthBanch": "develop"}),
        )

    def test_the_retired_config_layout_key_is_reported_as_unknown(self) -> None:
        """rulesetsDir and pythonConfig were read by nothing, so the pair was retired rather than wired (#1508).

        The unknown-key check is what makes the retirement stick: a re-added entry is reported rather than ignored.
        """
        self.assertIn(
            "Fixture: unknown key(s) configLayout",
            self.run_against(self.entry(configLayout={"rulesetsDir": "repo-config"})),
        )

    def test_a_well_formed_entry_reports_no_unknown_key(self) -> None:
        """The absence half of the check, over the fixture. The live registry's own keys are read by
        RegistrySchemaMirrorCase.test_the_gate_reads_its_key_sets_from_this_schema, which reads repos.json itself.
        """
        self.assertNotIn("unknown key(s)", self.run_against(self.entry()))


class GroundBranchReaderCase(unittest.TestCase):
    """Every reader of a repo's ground-truth branch resolves it through spec/audit.py's ground_branch_of().

    The defect this pins is a resolve that was correct and unreached. Each reader carried its own
    `entry.get("groundTruthBranch", "main")`, which consults no registry defaults, so a declared
    `defaults.groundTruthBranch` validated clean and was read by nothing. Reverting one call site reintroduces
    that at the site alone, and every behavioral test stays green, because the function is still correct and
    simply is not called with what it needs.

    Read as source rather than exercised, because five of the six call sites need a live GitHub API to reach.
    Only spec/workflow_reuse.py's is reachable offline, through the reader and lister its --selftest injects,
    and that selftest pins it behaviorally. spec/audit.py's ground_cases pin the function itself rather than
    any call site, which is the gap this case fills.

    A tripwire for the two shapes a revert actually takes, rather than a proof that no bypass exists. Source
    text cannot give the latter. What it does give is that dropping the defaults argument and resolving the key
    outside the function are both caught, and those are what a careless edit does.
    """

    READERS = ("spec/audit.py", "spec/fidelity_honesty.py", "spec/workflow_reuse.py")
    FUNCTION = "ground_branch_of"
    KEY = "groundTruthBranch"

    def parsed(self, rel: str) -> ast.Module:
        return ast.parse((validate.ROOT / rel).read_text(encoding="utf-8"))

    def own_definition(self, tree: ast.Module) -> ast.FunctionDef | None:
        """ground_branch_of's own body, the one place the key is resolved rather than read through it."""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == self.FUNCTION:
                return node
        return None

    def test_every_call_site_passes_the_registry_defaults(self) -> None:
        """Dropping the third argument is the natural revert, and it leaves the defaults step unreachable.

        `ground_branch_of(entry, branch)` still resolves entry-then-"main" and still passes every behavioral
        test, so nothing but this case tells it from the wired call.
        """
        calls = 0
        for rel in self.READERS:
            tree = self.parsed(rel)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name != self.FUNCTION:
                    continue
                calls += 1
                wired = len(node.args) >= 3 or any(k.arg == "defaults" for k in node.keywords)
                self.assertTrue(
                    wired,
                    f"{rel}:{node.lineno} calls {self.FUNCTION}() without the registry defaults, "
                    "so a declared defaults.groundTruthBranch is unread at that site",
                )
        # A floor rather than an exact count, so adding a reader does not fail this while removing them all does.
        # Without it, deleting every call site satisfies the loop above vacuously.
        self.assertGreaterEqual(calls, 6, "a reader stopped calling ground_branch_of() altogether")

    def test_no_reader_resolves_the_key_itself(self) -> None:
        """The other revert shape, and it is not one literal: `.get(key, "main")` and `.get(key) or "main"` and
        a bare subscript all resolve the field without the defaults step.

        Any read of the key outside the function's own body is refused rather than each shape enumerated, since
        an enumeration is exactly what the first two rounds of this pattern kept leaving a gap in.
        """
        for rel in self.READERS:
            tree = self.parsed(rel)
            definition = self.own_definition(tree)
            exempt = set()
            if definition is not None:
                exempt = {id(n) for n in ast.walk(definition)}
            # The self-tests read the key to build their own fixtures and to print what they resolved.
            # Neither is a production resolve, so they are exempt alongside the definition.
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("_selftest"):
                    exempt |= {id(n) for n in ast.walk(node)}
            for node in ast.walk(tree):
                if id(node) in exempt:
                    continue
                reads_key = False
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    reads_key = node.func.attr == "get" and any(
                        isinstance(a, ast.Constant) and a.value == self.KEY for a in node.args
                    )
                elif isinstance(node, ast.Subscript):
                    reads_key = (
                        isinstance(node.slice, ast.Constant) and node.slice.value == self.KEY
                    )
                self.assertFalse(
                    reads_key,
                    f"{rel}:{getattr(node, 'lineno', '?')} resolves '{self.KEY}' itself instead of "
                    f"through audit.{self.FUNCTION}(), so it consults no registry defaults",
                )

    def test_the_one_definition_resolves_all_three_steps(self) -> None:
        """A guard against the two cases above being satisfied by deleting the resolve rather than routing it."""
        definition = self.own_definition(self.parsed("spec/audit.py"))
        self.assertIsNotNone(definition, "spec/audit.py no longer defines ground_branch_of()")
        assert definition is not None  # narrows the type after the assertion above
        args = [a.arg for a in definition.args.args]
        self.assertEqual(args, ["entry", "branch", "defaults"])


class RegistrySchemaMirrorCase(unittest.TestCase):
    """registry/repos.schema.json is advisory, since no gate runs it, so its patterns answer to spec/validate.py.

    Two obligations, and they are different obligations. A pattern the schema copies from validate.py must stay
    byte-identical to it, or the editor and the gate drift apart silently. A pattern the schema states on its own
    must never refuse a value validate.py accepts, since an editor rejecting a valid registry is the direction
    #1504 reverted an earlier attempt for.
    """

    PORTABLE_END = r"(?![\s\S])"

    def setUp(self) -> None:
        self.schema = json.loads(
            (validate.ROOT / "registry" / "repos.schema.json").read_text(encoding="utf-8")
        )
        self.props = self.schema["$defs"]["repo"]["properties"]
        self.env_props = self.props["environments"]["items"]["properties"]

    def assert_portable(self, pattern: str) -> None:
        r"""The two constructs measured to mean different things in Python re and ECMA-262, refused by shape.

        `$` matches before a trailing newline in Python and only at the end in ECMA-262, so `"main\n"` satisfies a
        `$`-anchored pattern in one engine and not the other. `\s` and `\S` name different sets, ECMA-262's
        whitespace including U+FEFF where Python's does not. Their union is every character in both engines, which
        is what makes the `(?![\s\S])` terminator itself portable and why it is excluded from the body checked here.
        """
        self.assertTrue(pattern.startswith("^"), pattern)
        self.assertTrue(pattern.endswith(self.PORTABLE_END), pattern)
        body = pattern[: -len(self.PORTABLE_END)].replace(r"[\s\S]", "")
        for construct in ("$", "\\s", "\\S"):
            self.assertNotIn(construct, body, pattern)

    def test_the_copied_patterns_are_byte_identical_to_the_validator(self) -> None:
        for pattern, constant in (
            (self.schema["$defs"]["environmentName"]["pattern"], validate.ENVIRONMENT_NAME_PATTERN),
            (self.env_props["branches"]["items"]["pattern"], validate.DEPLOYMENT_BRANCH_PATTERN),
            (
                self.schema["$defs"]["groundTruthBranch"]["pattern"],
                validate.GROUND_TRUTH_BRANCH_PATTERN,
            ),
            (
                self.props["requiredSecrets"]["items"]["pattern"],
                validate.SECRET_NAME_PATTERN,
            ),
        ):
            with self.subTest(pattern=pattern):
                self.assertEqual(pattern, constant)

    def test_the_schema_declares_no_retired_key(self) -> None:
        """configLayout was retired rather than wired (#1508), and the gate's unknown-key check reads this list.

        A key left here would keep an entry declaring it validating clean, which is the state the retirement ended.
        """
        self.assertNotIn("configLayout", self.props)

    def test_the_gate_reads_its_key_sets_from_this_schema(self) -> None:
        """Every key the live registry declares resolves here, so the two objects the gate checks are in step."""
        registry = json.loads(
            (validate.ROOT / "registry" / "repos.json").read_text(encoding="utf-8")
        )
        declared: set[str] = {k for entry in registry["repos"] for k in entry}
        self.assertEqual(declared - set(self.props), set())
        defaults_props = set(self.schema["properties"]["defaults"]["properties"])
        self.assertEqual(set(registry.get("defaults", {})) - defaults_props, set())

    def test_both_ground_truth_branch_fields_reach_the_one_definition(self) -> None:
        """Two literals would let the defaults entry and the per-repo entry drift, and they feed the same readers."""
        ref = {"$ref": "#/$defs/groundTruthBranch"}
        self.assertEqual(self.props["groundTruthBranch"], ref)
        self.assertEqual(
            self.schema["properties"]["defaults"]["properties"]["groundTruthBranch"], ref
        )

    def every_pattern(self) -> set[str]:
        found: set[str] = set()

        def walk(node: object) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("pattern"), str):
                    found.add(node["pattern"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(self.schema)
        self.assertTrue(found)
        return found

    def test_every_pattern_in_the_schema_is_written_in_what_both_engines_share(self) -> None:
        for pattern in self.every_pattern():
            with self.subTest(pattern=pattern):
                self.assert_portable(pattern)

    def test_both_engines_agree_on_every_pattern_over_a_corpus(self) -> None:
        """assert_portable checks three constructs by shape, and this executes the patterns in the other engine.

        Node is what an editor's JSON language service uses, so it is the engine the schema actually meets. A host
        without node skips this rather than failing, since the shape check above still binds everywhere.
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed, so the ECMA-262 side cannot be executed here")
        patterns = sorted(self.every_pattern())
        corpus = [
            "pypi",
            "pypi ",
            " pypi",
            "pypi\n",
            "pypi\r\n",
            "\npypi",
            "pypi\u200b",
            "\ufeffpypi",
            "pypi\u00a0",
            "pypi\u180e",
            "pypi\u2060",
            "pypi\u00ad",
            "pypi\u2028",
            "pypi\u2029",
            "pypi\u1680",
            "pypi\u3000",
            "pypi\u0085",
            "",
            " ",
            "prod env",
            "releases/*",
            "main",
            "feature/*",
            "v1.*",
            "pypi\tx",
            "caf\u00e9",
            "a",
            "release/2.0",
            "main?ref=develop",
            "main?ref",
            "main#x",
            "main%2F",
            "/main",
            "main/",
            "main.",
            ".main",
            "a..b",
            "a/../b",
            "~x",
            "_wip",
            "wip-",
            "A short tagline.",
            "Runs at 40\u00b0C",
            "tabbed\t",
            "two\nlines",
        ]
        want = {p: [bool(re.search(p, c)) for c in corpus] for p in patterns}
        probe = (
            "const [pats, cases] = JSON.parse(process.argv[1]);"
            "console.log(JSON.stringify(Object.fromEntries("
            "pats.map(p => [p, cases.map(c => new RegExp(p).test(c))]))));"
        )
        result = subprocess.run(
            [node, "-e", probe, json.dumps([patterns, corpus])],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), want)

    def test_the_description_pattern_never_refuses_what_the_validator_accepts(self) -> None:
        """The schema's own statement rather than a copy, so the direction is what has to hold.

        A tier-2 or tier-3 non-ASCII character is legitimate in a description, and no portable positive grammar
        enumerates one, so this pattern states less than description_errors() does on purpose.
        """
        pattern = re.compile(self.props["description"]["pattern"])
        # Split rather than filtered by the validator's own verdict, so that every entry carries an assertion.
        # A later tightening of description_errors() then fails the split instead of silently retiring the entry it moved.
        accepted = [
            "A short tagline.",
            "Runs at 40 degrees C +/- 2",
            "Runs at 40\u00b0C",
            "A tagline with  two spaces",
            "zero\u200bwidth",
            "\ufeffbom",
        ]
        refused = [" padded", "padded ", "two\nlines", "trailing newline\n", "\ttabbed", "tabbed\t"]
        for desc in accepted:
            with self.subTest(accepted=desc):
                self.assertEqual(validate.description_errors("Fixture", desc), [])
                self.assertIsNotNone(pattern.search(desc))
        for desc in refused:
            with self.subTest(refused=desc):
                self.assertNotEqual(validate.description_errors("Fixture", desc), [])

    def test_the_description_pattern_refuses_a_trailing_newline(self) -> None:
        """The bug the `$` anchor carried: `$` matches just before one in Python, so check-jsonschema accepted a
        value description_errors() refuses, and an ECMA-262 editor refused the same value. Two engines, two answers.
        """
        pattern = self.props["description"]["pattern"]
        self.assertIsNone(re.compile(pattern).search("A short tagline.\n"))
        self.assertIsNotNone(re.compile("^\\S(?:[^\\n\\r]*\\S)?$").search("A short tagline.\n"))


class RegistryUrlIdentityCase(unittest.TestCase):
    """The url is what spec/audit.py turns into every request path, so one parse serves both."""

    def test_the_shapes_the_fleet_declares_resolve(self) -> None:
        self.assertEqual(
            validate.github_identity("https://github.com/ptr727/ProjectTemplate"),
            "ptr727/ProjectTemplate",
        )
        self.assertEqual(
            validate.github_identity("https://github.com/ptr727/ProjectTemplate/"),
            "ptr727/ProjectTemplate",
        )

    def test_a_url_the_consumer_could_not_address_resolves_to_none(self) -> None:
        for url in (
            "http://github.com/ptr727/ProjectTemplate",
            "https://github.com/ptr727",
            "https://gitlab.test/ptr727/ProjectTemplate",
            "https://github.com/ptr727/ProjectTemplate ",
            " https://github.com/ptr727/ProjectTemplate",
            "https://github.com/ptr727/ProjectTemplate\n",
            "https://github.com/ptr727/ProjectTemplate.git\n",
            "https://github.com/ptr727/Project Template",
            "https://github.com/ptr727:x/ProjectTemplate",
            "https://github.com/ptr727/ProjectTemplate?x=1",
            None,
            42,
        ):
            with self.subTest(url=url):
                self.assertIsNone(validate.github_identity(url))

    def test_a_dot_only_segment_resolves_to_none(self) -> None:
        """Measured against the live API: `repos/../rate_limit` returns 200 with the rate-limit document.

        GitHub decodes and then normalizes a dot segment, and `repos/%2e%2e/rate_limit` returns 200 the same way, so
        percent-encoding is no defense and the value has to be refused before it is ever addressed. The two positions
        are refused by different halves of the grammar, the owner by its character class and the repo by the guard
        after it, so both are listed here rather than left to whichever half happens to hold.
        """
        for url in (
            "https://github.com/../rate_limit",
            "https://github.com/./x",
            "https://github.com/../..",
            "https://github.com/a/..",
            "https://github.com/a/.",
            "https://github.com/a/...",
        ):
            with self.subTest(url=url):
                self.assertIsNone(validate.github_identity(url))

    def test_a_dot_bearing_name_that_is_not_a_dot_segment_still_resolves(self) -> None:
        """Only `.` and `..` normalize, so refusing every dot would refuse three names GitHub actually holds."""
        for url, want in (
            ("https://github.com/owner/.github", "owner/.github"),
            ("https://github.com/owner/v1.0", "owner/v1.0"),
            ("https://github.com/owner/a..b", "owner/a..b"),
        ):
            with self.subTest(url=url):
                self.assertEqual(validate.github_identity(url), want)

    def test_a_git_suffix_resolves_to_the_identity_the_consumer_addresses(self) -> None:
        """The suffix used to survive into the request. GITHUB_URL_RE makes `.git` optional and strips it for the
        identity, while spec/audit.py's repo_slug() took the last two raw path segments and addressed
        `repos/ptr727/ProjectTemplate.git/...`. One parse is what makes those the same string.
        """
        self.assertEqual(
            validate.github_identity("https://github.com/ptr727/ProjectTemplate.git"),
            "ptr727/ProjectTemplate",
        )


class AuditRepoSlugCase(unittest.TestCase):
    """spec/audit.py's repo_slug() is the consumer the url grammar exists for."""

    def setUp(self) -> None:
        # Imported here rather than at module scope, because spec/audit.py runs `git config --get remote.origin.url` while importing.
        # A module-level import would fail this whole file's cases on a host with no git on PATH, including every case that never touches audit.
        import audit

        self.audit = audit

    def test_a_git_suffixed_url_addresses_the_clean_slug(self) -> None:
        """Taking the last two path segments sent every read to `repos/owner/Fixture.git/...`."""
        self.assertEqual(
            self.audit.repo_slug({"url": "https://github.com/owner/Fixture.git"}), "owner/Fixture"
        )

    UNPARSEABLE = (
        "git@github.com:owner/Fixture.git",
        "http://github.com/owner/Fixture",
        "https://gitlab.test/owner/Fixture",
        "https://github.com/owner/Fixture?tab=readme",
        "https://github.com/owner/Fixture#readme",
        "https://github.com/../rate_limit",
    )

    def test_a_url_the_grammar_refuses_answers_with_a_sentinel_rather_than_raising(self) -> None:
        """spec/fidelity_honesty.py and spec/workflow_reuse.py call this on every entry with no handler.

        Raising would abort a whole fleet report over one malformed entry, which instead 404s and lands in the
        unreadable bucket beside its healthy siblings, so the gate refuses such a url and this stays generous.
        """
        for url in self.UNPARSEABLE:
            with self.subTest(url=url):
                self.assertIsNone(validate.github_identity(url))
                self.assertEqual(
                    self.audit.repo_slug({"url": url, "name": "Fixture"}),
                    f"{self.audit.UNRESOLVED_OWNER}/Fixture",
                )

    def test_no_fallback_slug_is_built_out_of_the_url_it_could_not_parse(self) -> None:
        """Taking the url's last two path segments produced a plausible slug rather than a failing one.

        `https://gitlab.test/owner/Repo` became `owner/Repo` and read that repository on github.com, and a `?` in the
        url survived into the value, where `repos/owner/Repo?tab=readme/branches/main` is a request to
        `repos/owner/Repo` with the rest as a query string. Both address something other than what was declared.
        """
        for url in self.UNPARSEABLE:
            with self.subTest(url=url):
                slug = self.audit.repo_slug({"url": url, "name": "Declared"})
                self.assertNotIn("owner", slug)
                self.assertNotIn("Fixture", slug)
                for char in ("?", "#"):
                    self.assertNotIn(char, slug, slug)

    def test_every_fallback_slug_is_two_segments_that_cannot_name_a_repository(self) -> None:
        """A one-segment slug shifts every later path component up one.

        `repos/Fixture/git/trees/<sha>` reads owner `Fixture` and repository `git`, and a name that is nothing but
        dots normalizes away, so `repos/<owner>/../branches/main` reads `repos/branches/main`. Both address something,
        which is worse than a 404 for a value that could not be parsed at all.
        """
        entries: list[dict[str, object]] = [
            {"url": u, "name": n}
            for u in self.UNPARSEABLE
            for n in ("Fixture", "..", ".", "A/B", "")
        ]
        entries += [{"name": "Fixture"}, {"url": None, "name": ".."}, {"url": 42}, {}]
        for entry in entries:
            with self.subTest(entry=entry):
                slug = self.audit.repo_slug(entry)
                owner, _, repo = slug.partition("/")
                self.assertEqual(owner, self.audit.UNRESOLVED_OWNER)
                self.assertIn(
                    "_", owner, "an owner GitHub could hold would collide with a real one"
                )
                self.assertNotEqual(repo, "")
                self.assertNotEqual(repo.strip("."), "", f"{slug} carries a dot segment")

    def test_the_branch_override_is_held_to_the_registry_grammar(self) -> None:
        """The override reaches the same path segment and `?ref=` value the declared field does.

        Run as the real script, since the guard lives in `main()` and `--selftest` returns before it. It precedes the
        first read, so this makes no network call.
        """
        for bad in ("a/../../../../../zen", "main?per_page=1", "main#x", "main "):
            with self.subTest(bad=bad):
                result = subprocess.run(
                    [sys.executable, str(validate.ROOT / "spec" / "audit.py"), "--branch", bad],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    cwd=validate.ROOT,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("does not address unencoded", result.stderr)

    def test_the_override_message_states_the_one_shape(self) -> None:
        """Two hand-written shape sentences would drift, and the grammar is stated in words exactly once."""
        result = subprocess.run(
            [sys.executable, str(validate.ROOT / "spec" / "audit.py"), "--branch", "main?x"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=validate.ROOT,
        )
        self.assertIn(validate.GROUND_TRUTH_BRANCH_SHAPE, result.stderr)
        self.assertIn(
            validate.GROUND_TRUTH_BRANCH_SHAPE,
            validate.ground_truth_branch_errors_for_repo({"groundTruthBranch": "main?x"}, "F")[0],
        )


class RegistryGroundTruthBranchCase(unittest.TestCase):
    """A declared groundTruthBranch is concatenated raw into a path segment and a `?ref=` query value."""

    def errors(self, value: object) -> list[str]:
        return validate.ground_truth_branch_errors_for_repo({"groundTruthBranch": value}, "Fixture")

    def test_an_absent_key_produces_no_errors(self) -> None:
        self.assertEqual(validate.ground_truth_branch_errors_for_repo({}, "Fixture"), [])

    def test_the_branch_names_a_fleet_repo_declares_are_accepted(self) -> None:
        for value in (
            "main",
            "develop",
            "release/2.0",
            "v1",
            "feature/JIRA-123",
            "_wip",
            "wip-",
            "a.b",
        ):
            with self.subTest(value=value):
                self.assertEqual(self.errors(value), [])

    def test_a_value_that_would_re_parse_the_url_is_rejected(self) -> None:
        """`?` and `#` do not fail the request, they send a different one, which is why the url field's own grammar
        excludes both and why a value landing in the same URLs has to as well.
        """
        for value in (
            "main?ref=develop",
            "main?ref",
            "main?",
            "main#x",
            "main#",
            "main branch",
            "main%2F",
            "main+x",
            "",
            "/main",
            "main/",
            "main.",
            ".main",
            "a..b",
            "..",
            "a/../b",
            "x/..",
            "~x",
            "main~1",
        ):
            with self.subTest(value=value):
                self.assertEqual(len(self.errors(value)), 1)

    def test_a_non_string_is_rejected_rather_than_read_as_absent(self) -> None:
        for value in (None, 42, ["main"]):
            with self.subTest(value=value):
                self.assertEqual(len(self.errors(value)), 1)


if __name__ == "__main__":
    unittest.main()
