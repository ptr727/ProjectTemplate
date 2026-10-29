#!/usr/bin/env python3
"""Drive lib/sh/secrets.sh's verdicts and env read against constructed files.

Each case builds its own tree under a temp root and asks the sourced library about it through a child bash.
A case therefore proves the function's answer for that shape rather than the state of this
host. A consumer carries the library byte-for-byte, so these cases are the only place it is tested.

Run as `python3 tests/test_secrets_sh.py`, or under `python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "lib" / "sh" / "secrets.sh"
TIMEOUT = 30
ROOT_ACCOUNT = hasattr(os, "geteuid") and os.geteuid() == 0


def run(
    script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source the library under the fleet header and run `script`, with `args` as `$1`..."""
    return subprocess.run(
        ["bash", "-c", f'set -Eeuo pipefail; . "$0"; {script}', str(LIBRARY), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=TIMEOUT,
        env=env,
        check=False,
    )


def answer(function: str, *args: str) -> str:
    result = run(f'{function} "$@"', *args)
    if result.returncode != 0:
        raise AssertionError(f"{function} exited {result.returncode}: {result.stderr}")
    return result.stdout.rstrip("\n")


def write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode())
    path.chmod(mode)
    return path


@unittest.skipUnless(sys.platform == "linux", "the library runs under bash on a Linux host")
class Tree(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # A temp root can itself sit below a symlink, such as /tmp on some hosts.
        # The walk reports a component by the spelling it was given.
        self.root = self.root.resolve()

    def tearDown(self) -> None:
        for current, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        self.temp.cleanup()


class SecretState(Tree):
    def test_absent_where_nothing_is_there(self) -> None:
        self.assertEqual(answer("secret_state", str(self.root / "missing")), "absent")

    def test_absent_below_a_missing_directory(self) -> None:
        self.assertEqual(answer("secret_state", str(self.root / "gone" / "key")), "absent")

    def test_present_for_a_regular_file(self) -> None:
        key = write(self.root / "key", "value\n")
        self.assertEqual(answer("secret_state", str(key)), "present")

    def test_doubled_and_trailing_slashes_name_the_same_file(self) -> None:
        write(self.root / "dir" / "key", "value\n")
        self.assertEqual(answer("secret_state", f"{self.root}//dir///key"), "present")

    def test_a_relative_path_is_unknown(self) -> None:
        self.assertEqual(
            answer("secret_state", "relative/key"), "unknown relative/key is not an absolute path"
        )

    def test_the_root_is_not_a_regular_file(self) -> None:
        self.assertEqual(answer("secret_state", "///"), "damaged / is not a regular file")

    def test_a_directory_leaf_is_damaged(self) -> None:
        (self.root / "dir").mkdir()
        self.assertEqual(
            answer("secret_state", str(self.root / "dir")),
            f"damaged {self.root}/dir is not a regular file",
        )

    def test_a_file_where_a_directory_belongs_is_damaged(self) -> None:
        write(self.root / "file", "")
        self.assertEqual(
            answer("secret_state", str(self.root / "file" / "key")),
            f"damaged {self.root}/file is not a directory, so it can hold no secret",
        )

    def test_a_dangling_leaf_link_is_damaged_rather_than_absent(self) -> None:
        (self.root / "key").symlink_to(self.root / "nowhere")
        self.assertTrue(
            answer("secret_state", str(self.root / "key")).startswith(
                f"damaged {self.root}/key is a symlink that does not resolve, so its target is missing"
            )
        )

    def test_a_dangling_parent_link_is_named(self) -> None:
        (self.root / "dir").symlink_to(self.root / "nowhere")
        self.assertTrue(
            answer("secret_state", str(self.root / "dir" / "key")).startswith(
                f"damaged {self.root}/dir is a symlink that does not resolve"
            )
        )

    def test_a_fifo_is_not_a_regular_file(self) -> None:
        os.mkfifo(self.root / "key")
        self.assertEqual(
            answer("secret_state", str(self.root / "key")),
            f"damaged {self.root}/key is not a regular file",
        )

    @unittest.skipIf(ROOT_ACCOUNT, "root enters a directory whatever its mode")
    def test_an_unenterable_directory_is_unknown_rather_than_absent(self) -> None:
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o600)
        self.assertEqual(
            answer("secret_state", str(locked / "key")),
            f"unknown {locked} cannot be entered by this account",
        )

    @unittest.skipIf(ROOT_ACCOUNT, "the root wording drops the unenterable-directory clause")
    def test_the_link_reason_names_an_unenterable_directory_for_a_non_root_account(self) -> None:
        self.assertTrue(
            answer("unresolved_link_reason").endswith(
                " sits behind a directory this account cannot enter"
            )
        )


class SecretVerdict(Tree):
    def test_a_private_clean_file_is_readable(self) -> None:
        key = write(self.root / "key", "value\n")
        self.assertEqual(answer("secret_verdict", str(key)), "readable")

    def test_absent_passes_through(self) -> None:
        self.assertEqual(answer("secret_verdict", str(self.root / "key")), "absent")

    def test_group_access_is_refused_by_default(self) -> None:
        key = write(self.root / "key", "value\n", 0o640)
        self.assertEqual(
            answer("secret_verdict", str(key)),
            f"refused {key} is mode 640, and must give group and other no access",
        )

    def test_shared_allows_group_and_other_read(self) -> None:
        key = write(self.root / "key", "value\n", 0o644)
        self.assertEqual(answer("secret_verdict", str(key), "shared"), "readable")

    def test_a_link_to_a_private_file_is_refused(self) -> None:
        target = write(self.root / "real", "value\n")
        (self.root / "key").symlink_to(target)
        self.assertEqual(
            answer("secret_verdict", str(self.root / "key")),
            f"refused {self.root}/key is a symlink, and must be the file itself",
        )

    def test_a_trailing_slash_is_refused_rather_than_normalized(self) -> None:
        key = write(self.root / "key", "value\n")
        self.assertEqual(
            answer("secret_verdict", f"{key}/"),
            f"refused {key}/ ends in a slash, so it names a directory rather than a file",
        )

    def test_a_carriage_return_is_refused(self) -> None:
        key = write(self.root / "key", "NAME=value\r\n")
        self.assertEqual(
            answer("secret_verdict", str(key)),
            f"refused {key} holds a carriage return, most often from CRLF line endings, "
            "and must use LF line endings only",
        )

    def test_a_damaged_state_becomes_a_refusal(self) -> None:
        (self.root / "dir").mkdir()
        self.assertEqual(
            answer("secret_verdict", str(self.root / "dir")),
            f"refused {self.root}/dir is not a regular file",
        )

    @unittest.skipIf(ROOT_ACCOUNT, "root reads a file whatever its mode")
    def test_an_unreadable_file_is_refused_with_its_owner(self) -> None:
        key = write(self.root / "key", "value\n", 0o000)
        self.assertTrue(
            answer("secret_verdict", str(key)).startswith(f"refused {key} is mode 0 and owned by ")
        )

    def test_a_fifo_content_check_is_unchecked(self) -> None:
        os.mkfifo(self.root / "fifo")
        self.assertEqual(answer("secret_content_state", str(self.root / "fifo")), "unchecked")


class SecretSetVerdict(Tree):
    def test_an_empty_set_is_readable(self) -> None:
        self.assertEqual(answer("secret_set_verdict"), "readable")

    def test_every_readable_file_makes_the_set_readable(self) -> None:
        one = write(self.root / "one", "a\n")
        two = write(self.root / "two", "b\n")
        self.assertEqual(answer("secret_set_verdict", str(one), str(two)), "readable")

    def test_a_missing_file_outranks_a_refusal_and_names_every_absence(self) -> None:
        refused = write(self.root / "refused", "a\n", 0o644)
        self.assertEqual(
            answer(
                "secret_set_verdict",
                str(refused),
                str(self.root / "first"),
                str(self.root / "second"),
            ),
            "missing first second",
        )

    def test_the_first_refusal_is_the_one_named(self) -> None:
        first = write(self.root / "first", "a\n", 0o644)
        second = write(self.root / "second", "b\r\n")
        self.assertEqual(
            answer("secret_set_verdict", str(first), str(second)),
            f"refused {first} is mode 644, and must give group and other no access",
        )


class RequireSecret(Tree):
    def test_a_readable_secret_succeeds_silently(self) -> None:
        key = write(self.root / "key", "value\n")
        result = run('require_secret "$1"', str(key))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_an_absent_secret_fails_and_says_so(self) -> None:
        result = run('require_secret "$1"', str(self.root / "key"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, f"ERROR: {self.root}/key is not there\n")

    def test_a_refused_secret_fails_with_the_reason(self) -> None:
        key = write(self.root / "key", "value\n", 0o604)
        result = run('require_secret "$1"', str(key))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr, f"ERROR: {key} is mode 604, and must give group and other no access\n"
        )

    def test_a_bare_call_stops_a_script_under_errexit(self) -> None:
        result = run('require_secret "$1"; echo reached', str(self.root / "key"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("reached", result.stdout)


class ReadEnvValue(Tree):
    def test_reads_the_named_value(self) -> None:
        env = write(self.root / "app.env", "APP_TOKEN=one\nAPP_HOST=two\n")
        self.assertEqual(answer("read_env_value", str(env), "APP_HOST"), "two")

    def test_an_unset_name_reads_empty(self) -> None:
        env = write(self.root / "app.env", "APP_TOKEN=one\n")
        self.assertEqual(answer("read_env_value", str(env), "APP_HOST"), "")

    def test_the_caller_environment_does_not_stand_in_for_the_file(self) -> None:
        env = write(self.root / "app.env", "APP_TOKEN=one\n")
        result = run(
            'read_env_value "$1" APP_HOST', str(env), env={**os.environ, "APP_HOST": "stale"}
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_a_failing_line_anywhere_fails_the_read(self) -> None:
        env = write(self.root / "app.env", "false\nAPP_HOST=two\n")
        result = run('read_env_value "$1" APP_HOST', str(env))
        self.assertNotEqual(result.returncode, 0)

    def test_a_failing_line_fails_inside_an_if_condition(self) -> None:
        env = write(self.root / "app.env", "false\nAPP_HOST=two\n")
        result = run(
            'if read_env_value "$1" APP_HOST; then echo ok; else echo failed; fi', str(env)
        )
        self.assertEqual(result.stdout, "failed\n")

    def test_what_the_file_prints_stays_out_of_the_value(self) -> None:
        env = write(self.root / "app.env", "echo noise\nAPP_HOST=two\n")
        self.assertEqual(answer("read_env_value", str(env), "APP_HOST"), "two")

    def test_the_file_cannot_redirect_the_read_through_the_positional_parameters(self) -> None:
        env = write(self.root / "app.env", "APP_HOST=two\nAPP_OTHER=wrong\nset -- x APP_OTHER\n")
        self.assertEqual(answer("read_env_value", str(env), "APP_HOST"), "two")

    def test_nothing_the_file_sets_reaches_the_caller(self) -> None:
        env = write(self.root / "app.env", "APP_TOKEN=one\nAPP_HOST=two\n")
        result = run(
            'read_env_value "$1" APP_HOST >/dev/null; printf "%s" "${APP_TOKEN-unset}"',
            str(env),
        )
        self.assertEqual(result.stdout, "unset")

    def test_bash_env_does_not_run_ahead_of_the_file(self) -> None:
        # A function shadows the builtin, so a hook that ran in the reading bash would answer for the file.
        # The outer bash runs it too, and loads the library with `.` rather than `source` for that reason.
        hook = write(self.root / "hook.sh", "source() { APP_HOST=injected; }\n")
        env = write(self.root / "app.env", "APP_TOKEN=one\n")
        result = run(
            'read_env_value "$1" APP_HOST', str(env), env={**os.environ, "BASH_ENV": str(hook)}
        )
        self.assertEqual((result.returncode, result.stdout), (0, ""))


class CarriedShfmtSettings(unittest.TestCase):
    def test_the_tree_pins_every_shfmt_setting_a_carrier_cannot_reformat_for(self) -> None:
        """A carrier's own `[*.sh]` must not reach the byte-locked copy `shfmt` checks.

        Each key `shfmt` reads is pinned at the value this library is formatted with.
        Only the one section is set, and no `root = true`, so every other setting stays the carrier's.
        """
        config = configparser.ConfigParser()
        config.read_string((LIBRARY.parent / ".editorconfig").read_text(encoding="utf-8"))
        self.assertEqual(config.sections(), ["*.sh"])
        self.assertEqual(
            dict(config["*.sh"]),
            {
                "binary_next_line": "false",
                "function_next_line": "false",
                "indent_size": "4",
                "indent_style": "space",
                "keep_padding": "false",
                "minify": "false",
                "never_split": "false",
                "shell_variant": "bash",
                "simplify": "false",
                "space_redirects": "false",
                "switch_case_indent": "false",
            },
        )


if __name__ == "__main__":
    unittest.main()
