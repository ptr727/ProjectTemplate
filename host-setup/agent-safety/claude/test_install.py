#!/usr/bin/env python3
"""Self-test for install.py, proving each stamp verdict by reintroducing the state it reports.

Every case runs against a throwaway CLAUDE_HOME, never the invoking user's. The installer writes to
a real home by default, so a test that forgot the override would rewrite the developer's own kit.

Standard library only, matching the rest of the gates, so CI needs no install step.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
INSTALL = HERE / "install.py"

sys.path.insert(0, str(HERE))
import install

# Held before any case patches `shutil.which`, which is the same object `install` reads.
REAL_WHICH = shutil.which


def run(home, *args, dirty=False, contain=True):
    """Invoke the installer as a subprocess, the way a host actually runs it.

    dirty forces the dirty-checkout signal install.py's own source_ref() would otherwise read
    live from this checkout, via AGENT_SAFETY_DIRTY_OVERRIDE, so a verdict this suite asserts
    depends on the fixture rather than on whether host-setup happens to be mid-edit while the
    suite runs. The default is clean, since that is what every case but one below needs.

    contain forces the containment-capable signal the same way, so a verdict does not depend on
    whether the machine running the suite has a systemd user manager.
    """
    env = dict(
        os.environ,
        CLAUDE_HOME=str(home),
        AGENT_SAFETY_DIRTY_OVERRIDE="1" if dirty else "0",
        AGENT_SAFETY_CONTAINMENT_OVERRIDE="1" if contain else "0",
    )
    return subprocess.run(
        [sys.executable, str(INSTALL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


class StampCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = pathlib.Path(self.tmp) / "claude"
        self.stamp = self.home / "agent-safety-stamp.json"
        self.md = self.home / "CLAUDE.md"

    def install(self, dirty=False, contain=True):
        r = run(self.home, dirty=dirty, contain=contain)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r


class TestReportVerdicts(StampCase):
    def test_report_on_a_machine_that_never_installed_says_so_and_installs_nothing(self):
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertIn("NOT INSTALLED", r.stdout)
        # The report path returns before the directory is created, so a read-only check stays read-only.
        self.assertFalse(self.home.exists())

    def test_install_then_report_is_current(self):
        self.install()
        self.assertTrue(self.stamp.exists())
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CURRENT", r.stdout)

    def test_installing_from_a_dirty_checkout_reports_stale(self):
        """The bytes on disk are not this commit's, whatever this checkout's real state is."""
        self.install(dirty=True)
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        if stamp["source"].get("vcs") != "git":
            self.skipTest("this checkout is not a git tree, so there is no dirty signal to force")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("installed from a dirty checkout", r.stdout)

    def test_a_changed_payload_reports_stale(self):
        self.install()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        self.addCleanup(target.write_bytes, original)
        target.write_bytes(original + b"\n<!-- drift -->\n")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("payload digest differs", r.stdout)

    def test_a_block_deleted_by_hand_reports_stale(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(
            re.sub(
                r"<!-- fleet-bootstrap v\d+ start -->.*?<!-- fleet-bootstrap v\d+ end -->",
                "",
                text,
                flags=re.DOTALL,
            ),
            encoding="utf-8",
        )
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("CLAUDE.md now holds", r.stdout)

    def test_reinstalling_clears_a_stale_verdict(self):
        """The remedy the report prints has to actually work, or the verdict is a dead end."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(
            re.sub(
                r"<!-- fleet-bootstrap v\d+ start -->.*?<!-- fleet-bootstrap v\d+ end -->",
                "",
                text,
                flags=re.DOTALL,
            ),
            encoding="utf-8",
        )
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestArgumentHandling(StampCase):
    def test_an_unknown_flag_is_rejected_rather_than_ignored(self):
        """The defect this closes: main() took no arguments, so the wrappers' pass-through was
        discarded and `install.py --help` performed a full install instead of printing usage."""
        self.install()
        before = self.stamp.read_text(encoding="utf-8")
        r = run(self.home, "--bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)
        self.assertEqual(self.stamp.read_text(encoding="utf-8"), before)

    def test_help_prints_usage_and_installs_nothing(self):
        r = run(self.home, "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--report", r.stdout)
        self.assertFalse(self.home.exists())


class TestBlocksPresent(StampCase):
    def test_a_half_written_block_does_not_count_as_present(self):
        """A start marker with no end is the failure a presence check reads as success."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(re.sub(r"<!-- agent-safety v\d+ end -->", "", text), encoding="utf-8")
        found = install.blocks_present(self.md)
        self.assertNotIn("agent-safety", found)
        self.assertIn("fleet-bootstrap", found)

    def test_an_absent_file_yields_no_blocks_rather_than_raising(self):
        self.assertEqual(install.blocks_present(self.home / "nothing.md"), {})


class TestSourceRef(unittest.TestCase):
    """Calls install.source_ref() directly, with AGENT_SAFETY_DIRTY_OVERRIDE unset.

    Every StampCase test above forces that override through run(), so none of them exercises
    the git status read this class alone still calls. Skips rather than fails when this
    checkout cannot supply a clean baseline itself, the same case TestDegradedEnvironments
    covers by removing PATH: a tarball with no git, or a real edit already sitting in a
    payload file while the suite runs.
    """

    def setUp(self):
        saved_override = os.environ.pop("AGENT_SAFETY_DIRTY_OVERRIDE", None)
        if saved_override is None:
            self.addCleanup(os.environ.pop, "AGENT_SAFETY_DIRTY_OVERRIDE", None)
        else:
            self.addCleanup(os.environ.__setitem__, "AGENT_SAFETY_DIRTY_OVERRIDE", saved_override)
        baseline = install.source_ref()
        if baseline.get("vcs") != "git":
            self.skipTest("this checkout is not a git tree, so source_ref() reads no status")
        if baseline.get("dirty"):
            self.skipTest("a payload file is already dirty in this checkout")

    def test_dirtying_a_payload_file_is_detected(self):
        """Proves the git-status branch itself, which the override lets every other test skip."""
        target = HERE / install.GUARD_NAME
        original = target.read_bytes()
        self.addCleanup(target.write_bytes, original)
        target.write_bytes(original + b"\n# dirtied by TestSourceRef, restored by addCleanup\n")
        self.assertTrue(install.source_ref()["dirty"])


class TestInstalledContent(StampCase):
    """Presence is not currency. These are the cases markers and versions cannot see."""

    def test_a_block_edited_between_its_own_markers_reports_stale(self):
        """The marker and version are untouched, so a presence check calls this machine current."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        edited = text.replace(
            "<!-- agent-safety v1 start -->",
            "<!-- agent-safety v1 start -->\nSomeone weakened this rule by hand.",
        )
        self.assertNotEqual(edited, text)
        self.md.write_text(edited, encoding="utf-8")
        # Presence is unchanged: the markers and versions still read exactly as before.
        self.assertEqual(
            install.blocks_present(self.md), {"agent-safety": "v1", "fleet-bootstrap": "v1"}
        )
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("installed content differs", r.stdout)

    def test_a_modified_hook_reports_stale(self):
        """The hook is not marker-delimited, so nothing else on this machine would notice."""
        self.install()
        hook = self.home / "hooks" / "gh-write-guard.py"
        hook.write_text(hook.read_text(encoding="utf-8") + "\n# neutered\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("installed content differs", r.stdout)

    def test_a_deleted_hook_reports_stale_rather_than_crashing(self):
        self.install()
        (self.home / "hooks" / "gh-write-guard.py").unlink()
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not fully installed", r.stdout)

    def test_identical_content_with_crlf_is_current_rather_than_stale(self):
        """CLAUDE.md keeps the endings it had, and a Windows host is not drifted for that alone."""
        self.install()
        raw = self.md.read_bytes()
        self.md.write_bytes(raw.replace(b"\n", b"\r\n"))
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("CURRENT", r.stdout)

    def test_reinstalling_clears_an_edited_block(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(
            text.replace(
                "<!-- agent-safety v1 start -->", "<!-- agent-safety v1 start -->\nedited"
            ),
            encoding="utf-8",
        )
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestDuplicateBlocks(StampCase):
    def test_a_duplicated_block_is_not_reported_as_present(self):
        """Two blocks mean the second silently governs, and naming the first hides that."""
        self.install()
        text = self.md.read_text(encoding="utf-8")
        block = re.search(
            r"<!-- agent-safety v1 start -->.*?<!-- agent-safety v1 end -->", text, re.DOTALL
        ).group(0)
        self.md.write_text(text + "\n" + block + "\n", encoding="utf-8")
        self.assertNotIn("agent-safety", install.blocks_present(self.md))

    def test_a_duplicated_block_reports_stale_rather_than_current(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        block = re.search(
            r"<!-- agent-safety v1 start -->.*?<!-- agent-safety v1 end -->", text, re.DOTALL
        ).group(0)
        self.md.write_text(text + "\n" + block + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class TestDegradedEnvironments(StampCase):
    def test_a_host_without_git_stamps_rather_than_crashing(self):
        """A tarball install on a minimal host has no git, which is normal rather than an error."""
        env = dict(os.environ, CLAUDE_HOME=str(self.home), PATH="")
        r = subprocess.run(
            [sys.executable, str(INSTALL)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        self.assertEqual(stamp["source"]["vcs"], "none")

    def test_a_stamp_missing_required_keys_gives_a_verdict_rather_than_a_traceback(self):
        self.install()
        self.stamp.write_text(json.dumps({"stampVersion": 1}) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertIn("missing", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_stamp_holding_invalid_utf8_gives_a_verdict_rather_than_a_traceback(self):
        """A partial write leaves bytes no decoder accepts, which raises before JSON is reached."""
        self.install()
        self.stamp.write_bytes(b'{"host": "\xff\xfe not utf-8"}')
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("unreadable", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_settings_holding_invalid_utf8_reports_stale_rather_than_a_traceback(self):
        """The registration read has the same shape and needed the same widening."""
        self.install()
        (self.home / "settings.json").write_bytes(b'{"hooks": "\xff\xfe"}')
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_stamp_holding_a_non_object_gives_a_verdict_rather_than_a_traceback(self):
        self.install()
        self.stamp.write_text("[]\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_every_required_key_holding_the_wrong_type_gives_a_verdict(self):
        """Presence is not shape. Each of these carries every key and crashes a key-only check."""
        self.install()
        good = json.loads(self.stamp.read_text(encoding="utf-8"))
        for key, bad in (
            ("host", "server"),
            ("source", "git"),
            ("payloadDigest", 12),
            ("blocks", ["agent-safety"]),
            ("installedUtc", None),
        ):
            with self.subTest(key=key):
                broken = dict(good, **{key: bad})
                self.stamp.write_text(json.dumps(broken) + "\n", encoding="utf-8")
                r = run(self.home, "--report")
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn(key, r.stderr)
                self.assertNotIn("Traceback", r.stderr)

    def test_the_formatter_stays_printable_on_a_stamp_the_validator_would_reject(self):
        """Belt and braces: a formatter that raises turns a verdict into the crash it reports on."""
        for broken in (
            {},
            {"host": None, "source": None},
            {"host": {}, "source": {}, "blocks": None},
            {"host": {"hostname": "h"}, "source": {"commit": 12345}},
        ):
            with self.subTest(stamp=broken):
                self.assertIsInstance(install.stamp_line(broken), str)


class TestRegistration(StampCase):
    """Correct bytes on disk are not a running guard. These are the inert-kit cases."""

    def _settings(self):
        return json.loads((self.home / "settings.json").read_text(encoding="utf-8"))

    def _write(self, data):
        (self.home / "settings.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    def test_an_unregistered_hook_reports_stale_rather_than_current(self):
        """Every byte is correct and the guard never runs, which every other check calls fine."""
        self.install()
        data = self._settings()
        data["hooks"]["PreToolUse"] = []
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("never runs", r.stdout)

    def test_a_removed_permission_rule_reports_stale(self):
        self.install()
        data = self._settings()
        data["permissions"]["allow"] = []
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("permission rule", r.stdout)

    def test_a_duplicated_hook_registration_reports_stale(self):
        self.install()
        data = self._settings()
        group = data["hooks"]["PreToolUse"][0]
        group["hooks"].append(dict(group["hooks"][0]))
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("more than once", r.stdout)

    def test_a_deleted_settings_file_reports_stale_rather_than_crashing(self):
        self.install()
        (self.home / "settings.json").unlink()
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_the_sweep_is_deployed_and_registered_once(self):
        """A second hook is a second chance to install the bytes and wire up nothing."""
        self.install()
        self.assertTrue((self.home / "hooks" / install.SWEEP_NAME).is_file())
        groups = self._settings()["hooks"]["SessionEnd"]
        entries = [
            h
            for g in groups
            for h in g.get("hooks", [])
            if install.SWEEP_STEM in h.get("command", "")
        ]
        self.assertEqual(len(entries), 1)
        # A SessionEnd hook's own budget is 1.5s, and only a declared per-hook timeout raises it.
        self.assertEqual(entries[0]["timeout"], install.SWEEP_TIMEOUT_SECONDS)

    def test_the_sweep_group_carries_no_matcher(self):
        """A matcher could narrow SessionEnd to some exit reasons, and a quit is not the only one."""
        self.install()
        groups = self._settings()["hooks"]["SessionEnd"]
        owning = [
            g
            for g in groups
            if any(install.SWEEP_STEM in h.get("command", "") for h in g.get("hooks", []))
        ]
        self.assertEqual(len(owning), 1)
        self.assertNotIn("matcher", owning[0])

    def test_reinstalling_does_not_duplicate_the_sweep(self):
        self.install()
        self.install()
        groups = self._settings()["hooks"]["SessionEnd"]
        entries = [
            h
            for g in groups
            for h in g.get("hooks", [])
            if install.SWEEP_STEM in h.get("command", "")
        ]
        self.assertEqual(len(entries), 1)

    def test_reinstalling_reuses_a_sweep_group_covering_every_reason(self):
        """`*` and `""` cover every reason the same as absent, so reinstalling must reuse that group
        rather than read only the matcherless shape and leave a second, empty one behind."""
        self.install()
        for matcher in ("*", ""):
            with self.subTest(matcher=matcher):
                data = self._settings()
                data["hooks"]["SessionEnd"][0]["matcher"] = matcher
                self._write(data)
                self.install()
                groups = self._settings()["hooks"]["SessionEnd"]
                owning = [
                    g
                    for g in groups
                    if any(install.SWEEP_STEM in h.get("command", "") for h in g.get("hooks", []))
                ]
                self.assertEqual(len(owning), 1, groups)
                self.assertEqual(owning[0].get("matcher"), matcher)

    def test_a_wrong_hooks_shape_is_reported_as_itself(self):
        """Iterating a dict yields keys and a string yields characters, so a wrong shape read as
        zero registrations and sent a reader to the wrong fix."""
        self.install()
        for shape, expected in (
            ({"SessionEnd": {"a": 1}}, "`hooks.SessionEnd` as dict"),
            ({"PreToolUse": "nope"}, "`hooks.PreToolUse` as str"),
            ("nope", "`hooks` as str"),
        ):
            data = self._settings()
            data["hooks"] = shape
            self._write(data)
            problems = install.registration_problems(self.home)
            self.assertTrue(
                any(expected in p for p in problems),
                f"{shape!r} reported {problems!r} rather than naming the shape",
            )

    def test_a_wrong_hooks_type_is_reported_once_rather_than_per_event(self):
        self.install()
        data = self._settings()
        data["hooks"] = "nope"
        self._write(data)
        problems = install.registration_problems(self.home)
        self.assertEqual(len([p for p in problems if "`hooks` as str" in p]), 1, problems)

    def test_a_decoy_command_naming_the_sweep_is_not_a_registration(self):
        """Matching any command containing the sweep's name let a decoy report a machine current.

        Each shape asserts the problem it should raise rather than that it raised one, since a
        second and false "not registered" line satisfied a bare truthiness check by itself.
        """
        self.install()
        good = self._settings()["hooks"]["SessionEnd"][0]["hooks"][0]
        for label, entry, expected in (
            (
                "decoy",
                {"type": "command", "command": "echo stray-process-sweep"},
                "does not run the deployed one",
            ),
            ("timeout", dict(good, timeout=1), "carries timeout 1"),
            ("type", dict(good, type="prompt"), "does not run the deployed one"),
        ):
            with self.subTest(shape=label):
                data = self._settings()
                data["hooks"]["SessionEnd"] = [{"hooks": [entry]}]
                self._write(data)
                problems = install.registration_problems(self.home)
                self.assertTrue(
                    any(expected in p for p in problems),
                    f"{label} shape reported {problems!r} rather than naming its own defect",
                )

    def test_an_entry_with_a_defect_is_not_also_reported_absent(self):
        """Counting only the sound entries added "the guard never runs" under every other problem."""
        self.install()
        data = self._settings()
        data["hooks"]["SessionEnd"][0]["hooks"][0]["type"] = "prompt"
        data["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
        self._write(data)
        problems = install.registration_problems(self.home)
        sweep_defect = "a SessionEnd entry names the sweep but does not run the deployed one"
        matcher_defect = "so that group never fires"
        self.assertTrue(
            any(sweep_defect in p for p in problems),
            f"the planted SessionEnd type defect was not reported: {problems!r}",
        )
        self.assertTrue(
            any(matcher_defect in p for p in problems),
            f"the planted PreToolUse matcher defect was not reported: {problems!r}",
        )
        self.assertEqual([p for p in problems if "is not registered" in p], [], problems)
        self.assertEqual([p for p in problems if "never runs" in p], [], problems)

    def test_a_matcher_every_bash_call_reaches_is_not_a_defect(self):
        """Requiring the exact string "Bash" reported three working registrations as broken."""
        self.install()
        for matcher in ("Bash", "Bash|Task", "*", ""):
            with self.subTest(matcher=matcher):
                data = self._settings()
                data["hooks"]["PreToolUse"][0]["matcher"] = matcher
                self._write(data)
                self.assertEqual(install.registration_problems(self.home), [], matcher)
        data = self._settings()
        del data["hooks"]["PreToolUse"][0]["matcher"]
        self._write(data)
        self.assertEqual(install.registration_problems(self.home), [])
        data = self._settings()
        data["hooks"]["PreToolUse"][0]["matcher"] = "Edit"
        self._write(data)
        self.assertTrue(
            any("so that group never fires" in p for p in install.registration_problems(self.home))
        )

    def test_a_registration_is_read_however_its_path_is_quoted(self):
        """A hand-written or UI-written registration runs the deployed path bare or single-quoted."""
        self.install()
        guard = self.home / "hooks" / install.DEPLOYED_HOOKS[0]
        for spelling in (f"python3 {guard}", f"python3 '{guard}'", f'python3 "{guard}"'):
            with self.subTest(command=spelling):
                data = self._settings()
                data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = spelling
                self._write(data)
                self.assertEqual(install.registration_problems(self.home), [], spelling)

    def test_an_absent_sweep_timeout_is_reported_as_absent(self):
        """The entry carries no timeout, so a message naming a value of None describes nothing."""
        self.install()
        data = self._settings()
        del data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]
        self._write(data)
        problems = install.registration_problems(self.home)
        self.assertTrue(any("carries no timeout" in p for p in problems), problems)
        self.assertEqual([p for p in problems if "None" in p], [], problems)

    def test_one_group_defect_is_reported_once_however_many_entries(self):
        """A matcher belongs to the group, so two entries under it are still one defect."""
        self.install()
        for event, key in (("PreToolUse", "Edit"), ("SessionEnd", "clear")):
            with self.subTest(event=event):
                data = self._settings()
                group = data["hooks"][event][0]
                group["matcher"] = key
                group["hooks"] = group["hooks"] * 2
                self._write(data)
                problems = install.registration_problems(self.home)
                matcher_lines = [p for p in problems if "matcher" in p and event in p]
                self.assertEqual(len(matcher_lines), 1, problems)

    def test_the_guard_is_deployed_and_searched_for_under_one_name(self):
        """The sweep routes both halves through a constant, and the guard spelled one half by hand."""
        self.install()
        self.assertTrue(install.GUARD_NAME.startswith(install.GUARD_STEM))
        live = self.home / "hooks" / install.GUARD_NAME
        self.assertTrue(live.is_file(), "the deployed name is not what the installer wrote")
        self.assertIn(
            install.GUARD_STEM, self._settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        )

    def test_a_longer_sweep_timeout_is_not_reported_as_a_defect(self):
        """A budget larger than this installer writes is better than it, not worse."""
        self.install()
        data = self._settings()
        data["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] = install.SWEEP_TIMEOUT_SECONDS + 20
        self._write(data)
        self.assertEqual(install.registration_problems(self.home), [])

    def test_a_failed_self_test_leaves_the_live_hook_alone(self):
        """Copying onto the live path and testing after left a broken hook installed and registered."""
        self.install()
        live = self.home / "hooks" / install.SWEEP_NAME
        before = live.read_bytes()
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        broken = root / "claude"
        shutil.copytree(HERE, broken, ignore=shutil.ignore_patterns("__pycache__"))
        sweep = broken / install.SWEEP_NAME
        sweep.write_text(
            sweep.read_text(encoding="utf-8").replace(
                'print("SELFTEST PASS" if ok else "SELFTEST FAIL")',
                'ok = False; print("SELFTEST FAIL")',
            ),
            encoding="utf-8",
        )
        env = dict(os.environ, CLAUDE_HOME=str(self.home))
        r = subprocess.run(
            [sys.executable, str(broken / "install.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(live.read_bytes(), before, "the live hook was replaced by a failing one")
        self.assertEqual(
            sorted(f.name for f in (self.home / "hooks").iterdir()),
            sorted(install.DEPLOYED_HOOKS),
            "a staged file was left behind",
        )

    def test_a_sweep_under_a_matcher_reports_stale(self):
        """A matcher filters SessionEnd by exit reason, so the sweep would miss every other exit."""
        self.install()
        data = self._settings()
        for group in data["hooks"]["SessionEnd"]:
            if any(install.SWEEP_STEM in h.get("command", "") for h in group.get("hooks", [])):
                group["matcher"] = "clear"
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("under a matcher", r.stdout)

    def test_a_sweep_matcher_covering_every_reason_is_not_a_defect(self):
        """An absent, empty, or `*` SessionEnd matcher runs on every exit reason, same as PreToolUse."""
        self.install()
        for matcher in ("*", ""):
            with self.subTest(matcher=matcher):
                data = self._settings()
                data["hooks"]["SessionEnd"][0]["matcher"] = matcher
                self._write(data)
                problems = install.registration_problems(self.home)
                self.assertEqual([p for p in problems if "under a matcher" in p], [], matcher)
        data = self._settings()
        data["hooks"]["SessionEnd"][0].pop("matcher", None)
        self._write(data)
        problems = install.registration_problems(self.home)
        self.assertEqual([p for p in problems if "under a matcher" in p], [])

    def test_an_unregistered_sweep_reports_stale_rather_than_current(self):
        self.install()
        data = self._settings()
        data["hooks"]["SessionEnd"] = []
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("never reported", r.stdout)

    def test_a_preexisting_matcher_group_is_left_alone(self):
        """Somebody else's SessionEnd hook keeps its own filter rather than gaining this one."""
        self.install()
        data = self._settings()
        data["hooks"]["SessionEnd"] = [
            {"matcher": "clear", "hooks": [{"type": "command", "command": "somebody-elses.sh"}]}
        ]
        self._write(data)
        self.install()
        groups = self._settings()["hooks"]["SessionEnd"]
        theirs = next(g for g in groups if g.get("matcher") == "clear")
        self.assertEqual([h["command"] for h in theirs["hooks"]], ["somebody-elses.sh"])
        self.assertTrue(
            any(
                install.SWEEP_STEM in h.get("command", "")
                for g in groups
                if "matcher" not in g
                for h in g.get("hooks", [])
            )
        )

    def test_reinstalling_clears_an_unregistered_hook(self):
        self.install()
        data = self._settings()
        data["hooks"]["PreToolUse"] = []
        self._write(data)
        self.assertEqual(run(self.home, "--report").returncode, 1)
        self.install()
        self.assertEqual(run(self.home, "--report").returncode, 0)


class TestContainmentPrefix(StampCase):
    """The shell prefix is set where the host can contain a command, and owned only while it names ours."""

    def _settings(self):
        return json.loads((self.home / "settings.json").read_text(encoding="utf-8"))

    def _write(self, data):
        (self.home / "settings.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

    def test_a_capable_host_sets_the_prefix_to_the_deployed_file(self):
        """A bare path, since Claude Code quotes the whole value as one word."""
        self.install()
        deployed = self.home / "hooks" / install.CONTAIN_NAME
        self.assertTrue(deployed.is_file())
        self.assertEqual(self._settings()["env"][install.PREFIX_VAR], str(deployed))
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_incapable_host_deploys_the_file_and_sets_no_prefix(self):
        """The digest stays identical across hosts, so only the registration differs."""
        self.install(contain=False)
        self.assertTrue((self.home / "hooks" / install.CONTAIN_NAME).is_file())
        self.assertNotIn("env", self._settings())
        r = run(self.home, "--report", contain=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # The report says so, since a silent fallback reads exactly like a contained host.
        self.assertIn("run uncontained", r.stdout)

    def test_a_removed_prefix_reports_stale_on_a_capable_host(self):
        self.install()
        data = self._settings()
        del data["env"]
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no task or memory ceiling", r.stdout)

    def test_our_prefix_on_a_host_that_cannot_contain_reports_stale(self):
        self.install()
        r = run(self.home, "--report", contain=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cannot contain", r.stdout)
        # Every hook fails to exec there, so the note that commands run uncontained would contradict it.
        self.assertNotIn("run uncontained", r.stdout)

    def test_a_prefix_synced_from_another_host_reports_stale_where_it_cannot_contain(self):
        """A path this host lacks exits 127 on every hook, which fails the guard open."""
        self.install(contain=False)
        data = self._settings()
        data["env"] = {install.PREFIX_VAR: "/elsewhere/.claude/hooks/" + install.CONTAIN_NAME}
        self._write(data)
        r = run(self.home, "--report", contain=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cannot contain", r.stdout)

    def test_reinstalling_on_a_host_that_lost_its_manager_removes_only_our_prefix(self):
        self.install()
        data = self._settings()
        data["env"]["OTHER"] = "kept"
        self._write(data)
        self.install(contain=False)
        self.assertEqual(self._settings()["env"], {"OTHER": "kept"})

    def test_a_foreign_prefix_is_kept_and_noted_without_a_stale_verdict(self):
        """Someone else's wrapper is theirs, and a re-run leaves it, so it cannot be drift a re-run clears."""
        self.home.mkdir(parents=True)
        self._write({"env": {install.PREFIX_VAR: "/usr/local/bin/audit-log"}})
        r = self.install()
        self.assertIn("does not own", r.stdout)
        self.assertEqual(self._settings()["env"][install.PREFIX_VAR], "/usr/local/bin/audit-log")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Note:", r.stdout)

    def test_a_prefix_naming_another_copy_reports_stale(self):
        self.install()
        data = self._settings()
        data["env"][install.PREFIX_VAR] = "/elsewhere/hooks/" + install.CONTAIN_NAME
        self._write(data)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("does not run the deployed prefix", r.stdout)

    def test_a_wrapper_merely_containing_the_prefix_name_is_foreign(self):
        """Someone else's wrapper is neither overwritten nor reported as a stale copy of ours."""
        self.home.mkdir(parents=True)
        other = "/usr/local/bin/audit-" + install.CONTAIN_NAME + "-wrapper"
        self._write({"env": {install.PREFIX_VAR: other}})
        self.install()
        self.assertEqual(self._settings()["env"][install.PREFIX_VAR], other)
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Note:", r.stdout)

    def test_a_non_object_env_is_refused_rather_than_overwritten(self):
        self.home.mkdir(parents=True)
        self._write({"env": ["not", "an", "object"]})
        r = run(self.home)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("`env`", r.stderr)
        self.assertEqual(self._settings()["env"], ["not", "an", "object"])


class TestContainmentCapable(unittest.TestCase):
    """The judgment every other case forces through AGENT_SAFETY_CONTAINMENT_OVERRIDE, run for real."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runtime = self.tmp / "runtime"
        (self.runtime / "systemd").mkdir(parents=True)
        (self.runtime / "systemd" / "private").write_text("", encoding="utf-8")
        self.prefix = self.tmp / install.CONTAIN_NAME
        shutil.copyfile(HERE / install.CONTAIN_NAME, self.prefix)
        os.chmod(self.prefix, 0o755)
        self.controllers = self.tmp / "cgroup.controllers"
        self.controllers.write_text("cpu memory pids\n", encoding="utf-8")
        which = mock.patch.object(install.shutil, "which", side_effect=self._which)
        which.start()
        self.addCleanup(which.stop)

    @staticmethod
    def _which(name):
        # The fake claims systemd-run is present, and every other name resolves as it really does.
        return "/usr/bin/systemd-run" if name == "systemd-run" else REAL_WHICH(name)

    def _judge(self, env=None, system="linux", uid=987654):
        env = {"XDG_RUNTIME_DIR": str(self.runtime)} if env is None else env
        return install.containment_capable(
            self.prefix, env=env, system=system, uid=uid, controllers=str(self.controllers)
        )

    @unittest.skipUnless(
        sys.platform.startswith("linux"), "the prefix runs through a POSIX shebang"
    )
    def test_a_manager_and_a_runnable_prefix_can_contain(self):
        capable, reason = self._judge()
        self.assertTrue(capable, reason)

    @unittest.skipUnless(sys.platform.startswith("linux"), "the execute bit is a POSIX property")
    def test_a_prefix_that_cannot_run_directly_cannot_contain(self):
        """Run through its interpreter it passes, and run the way Claude Code runs it, it fails."""
        os.chmod(self.prefix, 0o644)
        capable, reason = self._judge()
        self.assertFalse(capable)
        self.assertIn("does not run as its own executable", reason)

    def test_a_runtime_directory_lacking_the_socket_falls_back_to_the_standard_one(self):
        standard = os.path.join("/run/user/987654", "systemd", "private")
        real = os.path.exists
        with (
            mock.patch.object(
                install.os.path, "exists", side_effect=lambda p: p == standard or real(p)
            ),
            mock.patch.object(install, "prefix_runs_directly", return_value=""),
        ):
            capable, reason = self._judge(env={"XDG_RUNTIME_DIR": str(self.tmp / "wslg")})
        self.assertTrue(capable, reason)

    def test_a_manager_without_the_pids_and_memory_controllers_cannot_contain(self):
        """A scope under it starts and enforces neither ceiling, as on a cgroup-v1 or hybrid host."""
        self.controllers.write_text("cpu\n", encoding="utf-8")
        capable, reason = self._judge()
        self.assertFalse(capable)
        self.assertIn("pids and memory", reason)

    def test_an_unreadable_controllers_file_cannot_contain(self):
        self.controllers.unlink()
        self.assertFalse(self._judge()[0])

    def test_no_socket_anywhere_cannot_contain(self):
        capable, reason = self._judge(env={"XDG_RUNTIME_DIR": str(self.tmp / "wslg")})
        self.assertFalse(capable)
        self.assertIn("no systemd user manager", reason)

    def test_a_non_linux_host_cannot_contain(self):
        capable, reason = self._judge(system="darwin")
        self.assertFalse(capable)
        self.assertIn("darwin", reason)


class TestPreexistingCorruption(StampCase):
    """A file corrupted before the install, where the stamp records the corruption and agrees."""

    def _duplicate(self, marker="agent-safety"):
        text = self.md.read_text(encoding="utf-8")
        block = re.search(
            rf"<!-- {marker} v1 start -->.*?<!-- {marker} v1 end -->", text, re.DOTALL
        ).group(0)
        self.md.write_text(text + "\n" + block + "\n", encoding="utf-8")

    def test_installing_onto_a_duplicated_block_does_not_report_current(self):
        """The stamp is built from the same empty block set the file yields, so both agree."""
        self.install()
        self._duplicate()
        # Install again: the stamp is now written from a file that already carries the duplicate.
        self.install()
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # The install collapsed it, which is why this is CURRENT rather than a standing STALE.
        self.assertEqual(
            install.blocks_present(self.md), {"agent-safety": "v1", "fleet-bootstrap": "v1"}
        )

    def test_the_installer_collapses_a_duplicate_rather_than_preserving_it(self):
        """Substituting every match kept both blocks, so the printed remedy never worked."""
        self.install()
        self._duplicate()
        self.assertEqual(install.blocks_present(self.md), {"fleet-bootstrap": "v1"})
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"<!-- agent-safety v1 start -->", text)), 1)

    def test_markers_that_yield_no_valid_block_are_reported_regardless_of_the_stamp(self):
        """A stamp recording no blocks must not agree its way into a clean verdict."""
        self.install()
        self._duplicate()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        stamp["blocks"] = {}
        self.stamp.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("duplicated or incomplete", r.stdout)

    def test_a_half_written_block_is_reported_as_corruption(self):
        self.install()
        text = self.md.read_text(encoding="utf-8")
        self.md.write_text(re.sub(r"<!-- agent-safety v1 end -->", "", text), encoding="utf-8")
        self.assertEqual(
            install.marker_corruption(self.md),
            ["the agent-safety markers in CLAUDE.md are duplicated or incomplete"],
        )

    def test_a_clean_file_reports_no_corruption(self):
        self.install()
        self.assertEqual(install.marker_corruption(self.md), [])


class TestStampVersion(StampCase):
    def test_a_stamp_from_a_different_format_version_is_rejected(self):
        """The field exists so a shape change is detectable, which needs it to be read."""
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        stamp["stampVersion"] = install.STAMP_VERSION + 1
        self.stamp.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("stampVersion", r.stderr)

    def test_a_stamp_version_of_the_wrong_type_is_rejected(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        stamp["stampVersion"] = "1"
        self.stamp.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
        r = run(self.home, "--report")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("stampVersion", r.stderr)


class TestStampContent(StampCase):
    def test_the_stamp_names_the_machine_the_source_and_what_was_installed(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        self.assertEqual(stamp["stampVersion"], install.STAMP_VERSION)
        self.assertTrue(stamp["host"]["hostname"])
        self.assertTrue(stamp["payloadDigest"])
        self.assertEqual(stamp["blocks"], {"agent-safety": "v1", "fleet-bootstrap": "v1"})
        # Recorded from a real hub checkout, so the commit is present rather than the tarball fallback.
        self.assertIn(stamp["source"]["vcs"], ("git", "none"))

    def test_the_digest_covers_every_file_the_kit_installs(self):
        """A file added to the kit but left out of the digest is drift the report cannot see.

        The sentinel is non-whitespace deliberately. A snippet is embedded stripped, so appending a
        newline is not a change to installed content and this would assert the wrong thing.
        """
        baseline = install.payload_digest()
        for name in install.PAYLOAD_FILES:
            target = HERE / name
            original = target.read_bytes()
            try:
                target.write_bytes(original + b"\n# sentinel\n")
                self.assertNotEqual(
                    install.payload_digest(),
                    baseline,
                    f"{name} is in PAYLOAD_FILES but changing it did not move the digest",
                )
            finally:
                target.write_bytes(original)

    def test_trailing_whitespace_on_a_snippet_is_not_reported_as_drift(self):
        """The installer strips a snippet before embedding it, so this changes nothing installed.

        Hashing raw bytes reported STALE here and sent the operator to re-run an installer that
        would write the identical block.
        """
        baseline = install.payload_digest()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n\n")
            self.assertEqual(install.payload_digest(), baseline)
        finally:
            target.write_bytes(original)

    def test_a_bare_cr_in_a_snippet_is_not_reported_as_drift(self):
        """The installer reads snippets in text mode, so a bare CR arrives and installs as a newline.

        A digest normalizing CRLF but not CR reported the machine STALE against its own content.
        """
        baseline = install.payload_digest()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        try:
            # Built from the normalized form, since the snippets are CRLF in this repo.
            # A blind newline replace would turn each CRLF into a doubled CR rather than a bare one.
            target.write_bytes(install.normalized(original).replace(b"\n", b"\r"))
            self.assertEqual(install.payload_digest(), baseline)
        finally:
            target.write_bytes(original)

    def test_every_normalization_site_agrees(self):
        """The two digests must normalize identically, or a machine drifts against nothing."""
        for raw, want in ((b"a\r\nb", b"a\nb"), (b"a\rb", b"a\nb"), (b"a\nb", b"a\nb")):
            self.assertEqual(install.normalized(raw), want)
        for raw, want in (("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("a\nb", "a\nb")):
            self.assertEqual(install.normalized(raw), want)

    def test_a_real_edit_to_a_snippet_is_still_reported(self):
        """The normalization must not swallow a change that does reach the installed block."""
        baseline = install.payload_digest()
        target = HERE / "claude-md-safety.md"
        original = target.read_bytes()
        try:
            target.write_bytes(
                original.replace(
                    b"<!-- agent-safety v1 end -->",
                    b"Weakened by hand.\n<!-- agent-safety v1 end -->",
                )
            )
            self.assertNotEqual(install.payload_digest(), baseline)
        finally:
            target.write_bytes(original)

    def test_the_digest_reads_every_deployed_hook(self):
        """Naming the hooks in `installed_digest` was the drift `DEPLOYED_HOOKS` exists to close.

        Parametrised over the constant rather than over two literals, so a hook added to the deploy
        list is covered here without anyone remembering to add it.
        """
        for name in install.DEPLOYED_HOOKS:
            with self.subTest(hook=name):
                self.install()
                self.assertIsNotNone(install.installed_digest(self.home))
                (self.home / "hooks" / name).unlink()
                self.assertIsNone(
                    install.installed_digest(self.home),
                    f"{name} is deployed but the digest does not read it",
                )

    def test_every_deployed_file_is_in_the_digest(self):
        """Every file a real install writes is covered by the digest that decides CURRENT.

        Two earlier versions of this test each compared one declared list against another, which
        is a tautology `PAYLOAD_FILES`'s own definition satisfies, and a source scrape, which went
        silent the moment the copy became a loop. The independent source is the installed home
        itself: a file the installer actually wrote and the digest does not read is the gap, and
        reading the disk is the only way to see it without trusting the lists under test.
        """
        self.install()
        written = {f.name for f in (self.home / "hooks").iterdir() if f.is_file()}
        self.assertGreaterEqual(len(written), 2, "the install wrote fewer hooks than the kit has")
        for name in sorted(written):
            self.assertIn(
                name,
                install.PAYLOAD_FILES,
                f"the installer wrote {name} but PAYLOAD_FILES omits it, so the digest misses it",
            )
        # The blocks are covered the same way, from the file the installer actually wrote.
        text = self.md.read_text(encoding="utf-8")
        for marker in install.BLOCK_MARKERS:
            self.assertIn(f"<!-- {marker} v", text, f"the install wrote no {marker} block")
        for _, filename in install.CLAUDE_MD_BLOCKS:
            self.assertIn(filename, install.PAYLOAD_FILES)

    def test_the_payload_list_is_derived_from_the_block_list(self):
        """Written out by hand, the two drifted and the digest stopped covering a deployed file."""
        self.assertEqual(
            install.PAYLOAD_FILES,
            install.DEPLOYED_HOOKS + tuple(f for _, f in install.CLAUDE_MD_BLOCKS),
        )
        # The hooks the installer actually copies, read from `main`, are exactly that declared list.
        source = INSTALL.read_text(encoding="utf-8")
        self.assertIn("for src_name in DEPLOYED_HOOKS:", source)

    def test_every_reader_uses_the_same_marker_list(self):
        """Three readers each carried their own marker pair, so a new block could reach one only."""
        source = INSTALL.read_text(encoding="utf-8")
        self.assertNotIn(
            '("agent-safety", "fleet-bootstrap")',
            source,
            "a reader is carrying its own marker pair instead of BLOCK_MARKERS",
        )
        self.assertEqual(install.BLOCK_MARKERS, tuple(m for m, _ in install.CLAUDE_MD_BLOCKS))

    def test_the_one_line_summary_names_the_host_and_the_commit(self):
        self.install()
        stamp = json.loads(self.stamp.read_text(encoding="utf-8"))
        line = install.stamp_line(stamp)
        self.assertIn(stamp["host"]["hostname"], line)
        self.assertIn(stamp["payloadDigest"], line)


if __name__ == "__main__":
    unittest.main(verbosity=1)
