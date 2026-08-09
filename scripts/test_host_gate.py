#!/usr/bin/env python3
"""Tests for scripts/host_gate.py.

Two of these exist because the first live run produced them rather than because they were designed.
A probe that ran and failed was read as the tool being unreadable rather than absent, and the
remedy line under a floor failure was counted as a second issue. Both are cheap to assert and
neither was visible from reading the code.
"""
from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_gate  # noqa: E402


def tool(name, minimum=None, required=True, probes=None, pattern=r'v(\d+(?:\.\d+)*)', **extra):
    """A declaration entry, defaulted so a case states only the field it is about.

    The default probe is a Python one-liner rather than `true`, for two reasons. `true` is not a
    binary on native Windows, and it prints nothing, so a case that forgot to override `probes`
    would read as `unreadable` and pass or fail for a reason unrelated to what it tests. This one
    exists wherever the tests run and answers the default pattern.
    """
    entry = {
        'name': name,
        'required': required,
        'probes': probes or [[sys.executable, '-c', 'print("v1.0")']],
        'pattern': pattern,
        'minimum': minimum,
        'why': f'{name} exists for a reason.',
    }
    entry.update(extra)
    return entry


class TestVersionComparison(unittest.TestCase):
    def test_parses_dotted_integers(self):
        self.assertEqual(host_gate.parse_version('2.46.0'), (2, 46, 0))
        self.assertEqual(host_gate.parse_version('2025.08'), (2025, 8))

    def test_rejects_a_non_numeric_version(self):
        for bad in ('', '2.46.0-3', 'v2', 'latest', None):
            self.assertIsNone(host_gate.parse_version(bad), bad)

    def test_shorter_version_is_padded_not_ranked_lower(self):
        """`2025.08` and `2025.8.0` are the same version, so neither may win on component count."""
        self.assertEqual(host_gate.compare((2025, 8), (2025, 8, 0)), 0)
        self.assertEqual(host_gate.compare((2, 47), (2, 46, 9)), 1)
        self.assertEqual(host_gate.compare((2, 46, 0), (2, 47, 0)), -1)

    def test_the_real_pair_this_gate_exists_for(self):
        """gh 2.46.0 against its floor, and git-restore-mtime 2022.12 against its own."""
        self.assertLess(host_gate.compare(host_gate.parse_version('2.46.0'), host_gate.parse_version('2.47.0')), 0)
        self.assertLess(host_gate.compare(host_gate.parse_version('2022.12'), host_gate.parse_version('2025.08')), 0)


class TestProbe(unittest.TestCase):
    def test_a_failing_probe_is_not_an_answer(self):
        """The live bug: `git restore-mtime --version` prints git's error and exits 1 when absent.

        Reading that as output made the tool report unreadable, whose remedy is to fix the pattern,
        rather than absent, whose remedy is to install it.
        """
        self.assertIsNone(host_gate.probe([sys.executable, '-c', 'import sys; print("nope"); sys.exit(1)']))

    def test_a_missing_binary_is_not_an_answer(self):
        self.assertIsNone(host_gate.probe(['definitely-not-a-real-binary-xyzzy', '--version']))

    def test_a_version_on_stderr_is_an_answer(self):
        out = host_gate.probe([sys.executable, '-c', 'import sys; print("v9.9", file=sys.stderr)'])
        self.assertIsNotNone(out)
        self.assertIn('v9.9', out)


class TestReadTool(unittest.TestCase):
    def test_absent_when_no_probe_answers(self):
        status, version, _ = host_gate.read_tool(tool('ghost', probes=[['definitely-not-a-real-binary-xyzzy']]))
        self.assertEqual((status, version), ('absent', None))

    def test_unreadable_when_a_probe_answers_but_the_pattern_misses(self):
        entry = tool('odd', probes=[[sys.executable, '-c', 'print("no version here")']])
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ('unreadable', None))

    def test_the_second_probe_answers_when_the_first_does_not(self):
        """The platform whose interpreter is not called python3 is the case this exists for."""
        entry = tool('py', probes=[['definitely-not-a-real-binary-xyzzy'], [sys.executable, '-c', 'print("v3.13.5")']])
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ('read', '3.13.5'))

    def test_unreadable_names_the_probe_that_answered_not_the_first_declared(self):
        """Naming a probe that never ran sends the reader to fix a pattern against output they cannot reproduce."""
        entry = tool('odd', probes=[['definitely-not-a-real-binary-xyzzy'],
                                    [sys.executable, '-c', 'print("no version here")']])
        status, _, how = host_gate.read_tool(entry)
        self.assertEqual(status, 'unreadable')
        self.assertIn(sys.executable, how)
        self.assertNotIn('xyzzy', how)


class TestCheck(unittest.TestCase):
    def setUp(self):
        host_gate.NOTES.clear()

    def probe_for(self, text):
        return [[sys.executable, '-c', f'print({text!r})']]

    def test_a_version_below_the_floor_is_one_issue_not_two(self):
        """The remedy rides on the finding, so a source line does not inflate the count."""
        entry = tool('gh', minimum='2.47.0', probes=self.probe_for('v2.46.0'),
                     source={'linux': 'the official repo', 'macos': 'brew', 'windows': 'winget'})
        issues = host_gate.check([entry])
        self.assertEqual(len(issues), 1)
        self.assertIn('below the 2.47.0 floor', issues[0])
        self.assertIn('INSTALL FROM', issues[0])

    def test_a_version_at_the_floor_passes(self):
        self.assertEqual(host_gate.check([tool('gh', minimum='2.47.0', probes=self.probe_for('v2.47.0'))]), [])

    def test_an_absent_required_tool_fails_and_an_absent_optional_one_does_not(self):
        absent = [['definitely-not-a-real-binary-xyzzy']]
        self.assertEqual(len(host_gate.check([tool('needed', probes=absent)])), 1)
        self.assertEqual(host_gate.check([tool('extra', required=False, probes=absent)]), [])

    def test_no_floor_is_not_a_pass_dressed_as_a_check(self):
        """A tool with no floor produces a note naming the version, never a silent nothing."""
        self.assertEqual(host_gate.check([tool('docker', probes=self.probe_for('v29.7.1'))]), [])
        self.assertTrue(any('29.7.1' in n and 'no floor' in n for n in host_gate.NOTES))

    def test_a_malformed_floor_is_reported_rather_than_skipped(self):
        """Skipping it would retire the check while the run stayed green."""
        issues = host_gate.check([tool('bad', minimum='2.x', probes=self.probe_for('v3.0'))])
        self.assertEqual(len(issues), 1)
        self.assertIn('nothing could be compared', issues[0])


class TestMerge(unittest.TestCase):
    """A repository layers its own requirements over the hub's, and may only tighten them."""

    def base(self):
        return [tool('gh', minimum='2.47.0'), tool('git-restore-mtime', minimum='2025.08', required=False)]

    def test_a_repo_may_turn_an_optional_tool_required(self):
        merged, rejected = host_gate.merge(self.base(), [{'name': 'git-restore-mtime', 'required': True}])
        self.assertEqual(rejected, [])
        self.assertTrue(next(t for t in merged if t['name'] == 'git-restore-mtime')['required'])

    def test_a_repo_may_raise_a_floor(self):
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'minimum': '2.60.0'}])
        self.assertEqual(rejected, [])
        self.assertEqual(next(t for t in merged if t['name'] == 'gh')['minimum'], '2.60.0')

    def test_a_repo_may_not_lower_a_floor(self):
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'minimum': '2.10.0'}])
        self.assertEqual(next(t for t in merged if t['name'] == 'gh')['minimum'], '2.47.0')
        self.assertEqual(len(rejected), 1)
        self.assertIn('lower the floor', rejected[0])

    def test_a_repo_may_not_remove_a_floor(self):
        """Null is the relaxation that looks least like one, so it is asserted on its own."""
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'minimum': None}])
        self.assertEqual(next(t for t in merged if t['name'] == 'gh')['minimum'], '2.47.0')
        self.assertEqual(len(rejected), 1)

    def test_a_repo_may_not_turn_a_required_tool_optional(self):
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'required': False}])
        self.assertTrue(next(t for t in merged if t['name'] == 'gh').get('required', True))
        self.assertEqual(len(rejected), 1)
        self.assertIn('required tool optional', rejected[0])

    def test_a_repo_may_add_a_tool_the_fleet_does_not_have(self):
        ffmpeg = tool('ffmpeg', minimum='6.0', pattern=r'ffmpeg version (\d+(?:\.\d+)*)')
        merged, rejected = host_gate.merge(self.base(), [ffmpeg])
        self.assertEqual(rejected, [])
        self.assertIn('ffmpeg', [t['name'] for t in merged])

    def test_a_new_tool_missing_fields_is_rejected_rather_than_half_added(self):
        """An entry the gate cannot probe is worth less than no entry, so it never enters the set."""
        merged, rejected = host_gate.merge(self.base(), [{'name': 'ffmpeg', 'minimum': '6.0'}])
        self.assertNotIn('ffmpeg', [t['name'] for t in merged])
        self.assertEqual(len(rejected), 1)
        self.assertIn('without', rejected[0])

    def test_a_case_variant_overrides_rather_than_adding_a_second_entry(self):
        """Keying on the exact spelling would turn a misspelled override into an addition, and report success."""
        merged, rejected = host_gate.merge(self.base(), [{'name': 'GH', 'minimum': '2.60.0'}])
        self.assertEqual(rejected, [])
        self.assertEqual(len(merged), 2)
        gh = next(t for t in merged if t['name'].lower() == 'gh')
        self.assertEqual(gh['minimum'], '2.60.0')
        self.assertEqual(gh['name'], 'gh', 'the hub spelling stands, since the gate reports under it')

    def test_a_case_variant_cannot_relax_either(self):
        """Otherwise folding case would open the door the tighten-only rule closes."""
        merged, rejected = host_gate.merge(self.base(), [{'name': 'GH', 'required': False}])
        self.assertEqual(len(rejected), 1)
        self.assertTrue(next(t for t in merged if t['name'] == 'gh').get('required', True))

    def test_a_falsy_non_boolean_cannot_relax_required(self):
        """`0` is falsy and is not False, so a guard written against the literal would let it through."""
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'required': 0}])
        self.assertTrue(next(t for t in merged if t['name'] == 'gh').get('required', True))
        self.assertEqual(len(rejected), 1)
        self.assertIn('required must be true or false', rejected[0])

    def test_a_numeric_minimum_is_refused_rather_than_raising(self):
        merged, rejected = host_gate.merge(self.base(), [{'name': 'gh', 'minimum': 5}])
        self.assertEqual(next(t for t in merged if t['name'] == 'gh')['minimum'], '2.47.0')
        self.assertEqual(len(rejected), 1)
        self.assertIn('dot-separated integers', rejected[0])

    def test_a_new_tool_with_a_present_but_unusable_field_is_refused(self):
        """The field is there, so a presence check passes it and the later read is what crashes."""
        entry = tool('ffmpeg', minimum='6.0')
        entry['probes'] = None
        merged, rejected = host_gate.merge(self.base(), [entry])
        self.assertNotIn('ffmpeg', [t['name'] for t in merged])
        self.assertIn('probes must be', rejected[0])

    def test_an_uncompilable_pattern_is_refused(self):
        entry = tool('ffmpeg', minimum='6.0', pattern='(unclosed')
        merged, rejected = host_gate.merge(self.base(), [entry])
        self.assertNotIn('ffmpeg', [t['name'] for t in merged])
        self.assertIn('does not compile', rejected[0])

    def test_the_hub_declaration_is_not_mutated(self):
        """The caller's list is reused across runs, so merging must copy rather than edit in place."""
        base = self.base()
        host_gate.merge(base, [{'name': 'gh', 'minimum': '2.60.0'}])
        self.assertEqual(base[0]['minimum'], '2.47.0')


class TestContractAfterLayering(unittest.TestCase):
    """A declared floor carries a usable source, read on the merged set.

    The case that motivates it: a repository adds a floor to a hub entry that never had a source.
    Both files are correct alone, the combination is not, and the symptom is a failure with no
    remedy printed.
    """

    def test_a_floor_with_no_source_is_reported(self):
        merged, _ = host_gate.merge([tool('docker', minimum=None)], [{'name': 'docker', 'minimum': '20.0'}])
        problems = host_gate.contract_problems(merged)
        self.assertEqual(len(problems), 1)
        self.assertIn('no source', problems[0])

    def test_a_new_local_tool_with_a_floor_and_no_source_is_reported(self):
        merged, rejected = host_gate.merge([], [tool('ffmpeg', minimum='6.0')])
        self.assertEqual(rejected, [])
        self.assertEqual(len(host_gate.contract_problems(merged)), 1)

    def test_a_floor_with_a_source_is_not_reported(self):
        merged, _ = host_gate.merge([tool('docker', minimum=None)],
                                    [{'name': 'docker', 'minimum': '20.0', 'source': {'linux': 'from here'}}])
        self.assertEqual(host_gate.contract_problems(merged), [])

    def test_a_source_key_no_platform_reads_is_reported(self):
        self.assertEqual(len(host_gate.contract_problems(
            [tool('gh', minimum='2.47.0', source={'Linux': 'from here'})])), 1)

    def test_an_empty_source_value_is_reported(self):
        problems = host_gate.contract_problems([tool('gh', minimum='2.47.0', source={'linux': ''})])
        self.assertEqual(len(problems), 1)
        self.assertIn('source.linux is empty', problems[0])

    def test_no_floor_needs_no_source(self):
        self.assertEqual(host_gate.contract_problems([tool('docker', minimum=None)]), [])

    def test_the_shipped_declaration_meets_its_own_contract(self):
        data = json.loads(host_gate.SPEC.read_text(encoding='utf-8'))
        self.assertEqual(host_gate.contract_problems(data['tools']), [])


class TestUncompilablePatternAtReadTime(unittest.TestCase):
    """The backstop for a pattern that reached read_tool without either shape check having run."""

    def test_a_bad_pattern_reports_unreadable_rather_than_raising(self):
        entry = tool('odd', pattern='(unclosed', probes=[[sys.executable, '-c', 'print("v1.0")']])
        status, version, _ = host_gate.read_tool(entry)
        self.assertEqual((status, version), ('unreadable', None))


class TestMalformedLocalFile(unittest.TestCase):
    """A repository file is validated by nothing upstream, so the gate reports rather than crashes."""

    def run_against(self, tmp, payload):
        (tmp / 'host-tools.json').write_text(payload, encoding='utf-8')
        return host_gate.main(['--repo', str(tmp), '--quiet'])

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
            self.assertEqual(self.run_against(Path(d), '{not json'), 2)

    def test_a_top_level_that_is_not_an_object_exits_two(self):
        """Indexing a list or a null with a string raises TypeError, which is not a ValueError."""
        import tempfile
        for payload in ('[]', 'null', '"a string"', '3'):
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(self.run_against(Path(d), payload), 2, payload)


class TestMalformedHubFile(unittest.TestCase):
    """The same hole on the hub read, which the docstring says exits 2 with a diagnostic."""

    def test_a_top_level_that_is_not_an_object_exits_two(self):
        import tempfile
        for payload in ('[]', 'null', '3'):
            with tempfile.TemporaryDirectory() as d:
                spec = Path(d) / 'host-tools.json'
                spec.write_text(payload, encoding='utf-8')
                self.assertEqual(host_gate.main(['--spec', str(spec), '--no-local', '--quiet']), 2, payload)

    def test_unparsable_hub_json_exits_two(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d) / 'host-tools.json'
            spec.write_text('{not json', encoding='utf-8')
            self.assertEqual(host_gate.main(['--spec', str(spec), '--no-local', '--quiet']), 2)

    def test_tools_that_is_not_an_array_of_objects_exits_two(self):
        """The shape guard existed on the local file and not on this one, which is the asymmetry."""
        import tempfile
        for payload in ('{"tools": {"gh": {}}}', '{"tools": "gh"}', '{"tools": ["gh"]}', '{"tools": [null]}'):
            with tempfile.TemporaryDirectory() as d:
                spec = Path(d) / 'host-tools.json'
                spec.write_text(payload, encoding='utf-8')
                self.assertEqual(host_gate.main(['--spec', str(spec), '--no-local', '--quiet']), 2, payload)


class TestReadDeclaration(unittest.TestCase):
    """One reader for both files, so a guard cannot be present on one and absent on the other."""

    def read(self, payload):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'host-tools.json'
            p.write_text(payload, encoding='utf-8')
            return host_gate.read_declaration(p, 'declaration')

    def test_a_good_file_returns_the_entries(self):
        got = self.read('{"tools": [{"name": "gh"}]}')
        self.assertIsInstance(got, list)
        self.assertEqual(got[0]['name'], 'gh')

    def test_every_bad_shape_returns_a_diagnostic_string(self):
        for payload in ('[]', 'null', '3', '{"no tools": 1}', '{not json',
                        '{"tools": {"gh": {}}}', '{"tools": ["gh"]}', '{"tools": [null]}'):
            self.assertIsInstance(self.read(payload), str, payload)

    def test_a_missing_file_is_a_diagnostic_rather_than_a_raise(self):
        self.assertIsInstance(host_gate.read_declaration(Path('/definitely/not/here.json'), 'declaration'), str)


class TestShippedDeclaration(unittest.TestCase):
    """The file this repo actually ships, read rather than assumed."""

    def setUp(self):
        self.data = json.loads(host_gate.SPEC.read_text(encoding='utf-8'))

    def test_every_entry_carries_the_required_fields(self):
        for t in self.data['tools']:
            for field in host_gate.REQUIRED_FIELDS:
                self.assertIn(field, t, f'{t.get("name")} is missing {field}')

    def test_every_declared_floor_parses(self):
        for t in self.data['tools']:
            if t['minimum'] is not None:
                self.assertIsNotNone(host_gate.parse_version(t['minimum']), t['name'])

    def test_a_floor_carries_a_source_so_the_finding_names_a_remedy(self):
        for t in self.data['tools']:
            if t['minimum'] is not None:
                self.assertTrue(t.get('source'), f'{t["name"]} declares a floor and no source to install from')

    def test_the_two_known_defects_are_the_declared_floors(self):
        """A floor exists only where a defect is known, so this asserts the set rather than a count."""
        floors = {t['name'] for t in self.data['tools'] if t['minimum'] is not None}
        self.assertEqual(floors, {'gh', 'git-restore-mtime'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
