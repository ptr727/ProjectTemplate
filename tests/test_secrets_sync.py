#!/usr/bin/env python3
"""Exercise secrets_sync.py's guards against a fake `op` holding in-memory vaults.

The cases drive the real subcommands through `main`, with `run_op` replaced by the fake, and a
few call a parser directly. The properties under test are what the tool decides and in what order
it writes, not what `op` does. The interrupt cases are the exception. They run the script under its interpreter with an
executable stub `op` on PATH, since only a real child can show it is stopped.

Every name, title, and value here is constructed. No case reads the real home directory.

Run as `python3 tests/test_secrets_sync.py`, or under
`python3 -m unittest discover -s tests`.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from collections.abc import Mapping, Sequence
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "secrets_sync.py"
sys.path.insert(0, str(SCRIPT.parent))
import secrets_sync as ss

REAL_RUN_OP = ss.run_op

SECRET = b"alpha-constructed-secret-value\n"
POSIX_ONLY = unittest.skipIf(sys.platform == "win32", "POSIX modes and symbolic links")
WRITE_KEYS = frozenset(
    {
        ("vault", "create"),
        ("vault", "delete"),
        ("document", "create"),
        ("document", "edit"),
        ("item", "edit"),
        ("item", "delete"),
    }
)
OK = 0
FAILED = ss.Outcome(1, b"", b"constructed failure")


def opt(argv: Sequence[str], flag: str) -> str:
    return argv[list(argv).index(flag) + 1]


def key_of(argv: Sequence[str]) -> tuple[str, ...]:
    return (argv[0],) if argv[0] in {"whoami", "signin", "signout"} else tuple(argv[:2])


class FakeOp:
    """An in-memory `op` answering the argument lists the tool sends, recording each call."""

    def __init__(self) -> None:
        self.accounts = 1
        self.ambient = True
        self.token: str | None = None
        self.vaults: dict[str, str] = {}
        self.items: dict[str, dict[str, dict[str, object]]] = {}
        self.calls: list[tuple[list[str], dict[str, str], bool]] = []
        self.ids = itertools.count(1)
        self.minute = 0
        self.corrupt = False
        self.fail_before: set[tuple[str, ...]] = set()
        self.fail_after: set[tuple[str, ...]] = set()
        self.raise_on: dict[tuple[str, ...], BaseException] = {}
        self.signin_output = (
            b'# a constructed comment line\nexport OP_SESSION_alpha="constructed-token"\n'
        )
        self.signin_token = "constructed-token"
        self.edit_moves_id = False
        self.merge_tags = False
        self.archive_noop = False
        self.omit_updated = False
        self.signin_ambient = False
        self.drop_create = False
        self.raw: dict[tuple[str, ...], bytes] = {}
        self.vault_create_noop = False

    def add_vault(self, name: str) -> str:
        vid = f"v{next(self.ids)}"
        self.vaults[vid] = name
        self.items[vid] = {}
        return vid

    def stamp(self) -> str:
        self.minute += 1
        return f"2020-01-01T00:{self.minute:02d}:00Z"

    def add_item(
        self,
        vid: str,
        title: str,
        content: bytes,
        tags: Sequence[str] = ("mode:0600",),
        updated: str = "",
    ) -> str:
        iid = f"i{next(self.ids)}"
        self.items[vid][iid] = {
            "title": title,
            "content": content,
            "tags": tuple(tags),
            "updated_at": updated or self.stamp(),
        }
        return iid

    def keys(self) -> list[tuple[str, ...]]:
        return [key_of(argv) for argv, _, _ in self.calls]

    def writes(self) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if key_of(argv) in WRITE_KEYS]

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], attached: bool) -> ss.Outcome:
        argv = list(argv)
        self.calls.append((list(argv), dict(env), attached))
        if "--account" in argv:
            index = argv.index("--account")
            del argv[index : index + 2]
        key = key_of(argv)
        if key in self.raise_on:
            raise self.raise_on[key]
        if key in self.fail_before:
            return FAILED
        if key in self.raw:
            return ss.Outcome(0, self.raw[key], b"")
        result = self.dispatch(key, argv, env)
        return FAILED if key in self.fail_after else result

    def dispatch(self, key: tuple[str, ...], argv: list[str], env: Mapping[str, str]) -> ss.Outcome:
        def ok(out: bytes = b"") -> ss.Outcome:
            return ss.Outcome(0, out, b"")

        if key == ("account", "list"):
            return ok(json.dumps([{}] * self.accounts).encode())
        if key == ("whoami",):
            signed_in = self.token is not None and env.get("OP_SESSION_alpha") == self.token
            return ok(b"constructed-user") if self.ambient or signed_in else FAILED
        if key == ("signin",):
            self.token = self.signin_token
            self.ambient = self.ambient or self.signin_ambient
            return ok(self.signin_output)
        if key == ("signout",):
            self.token = None
            return ok()
        if key == ("vault", "list"):
            return ok(json.dumps([{"id": i, "name": n} for i, n in self.vaults.items()]).encode())
        if key == ("vault", "create"):
            if not self.vault_create_noop:
                self.add_vault(argv[2])
            return ok()
        if key == ("vault", "delete"):
            self.vaults.pop(argv[2])
            self.items.pop(argv[2])
            return ok()
        vault = self.items.get(opt(argv, "--vault"), {})
        if key == ("item", "list"):
            rows = []
            for iid, it in vault.items():
                row: dict[str, object] = {
                    "id": iid,
                    "title": it["title"],
                    "updated_at": it["updated_at"],
                }
                if self.omit_updated:
                    del row["updated_at"]
                tags = it["tags"]
                if tags:
                    row["tags"] = list(tags) if isinstance(tags, tuple) else tags
                rows.append(row)
            return ok(json.dumps(rows).encode())
        if key == ("document", "create") and self.drop_create:
            return ok()
        if key == ("document", "create"):
            tags = opt(argv, "--tags").split(",") if "--tags" in argv else []
            self.add_item(
                opt(argv, "--vault"), opt(argv, "--title"), Path(argv[2]).read_bytes(), tags
            )
            return ok()
        if key == ("document", "edit"):
            vault[argv[2]].update(content=Path(argv[3]).read_bytes(), updated_at=self.stamp())
            return ok()
        if key == ("document", "get"):
            got = vault.get(argv[2])
            if got is None:
                return FAILED
            content = got["content"]
            assert isinstance(content, bytes)
            return ok(content + (b"corrupted" if self.corrupt else b""))
        if key == ("item", "edit"):
            tags = tuple(opt(argv, "--tags").split(","))
            previous = vault[argv[2]]["tags"]
            if self.merge_tags and isinstance(previous, tuple):
                tags = (*previous, *tags)
            vault[argv[2]].update(tags=tags, updated_at=self.stamp())
            if self.edit_moves_id:
                vault[f"i{next(self.ids)}"] = vault.pop(argv[2])
            return ok()
        if key == ("item", "delete"):
            assert "--archive" in argv, argv
            if not self.archive_noop:
                vault.pop(argv[2])
            return ok()
        raise AssertionError(f"unexpected op call {argv}")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeOp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.home = self.scratch / "home"
        self.home.mkdir(mode=0o700)
        self.reply = True
        self.asked: list[str] = []
        self.tty = True
        self.enterContext(unittest.mock.patch.object(ss, "run_op", self.fake))
        self.enterContext(unittest.mock.patch.object(ss, "home_dir", lambda: self.home))
        self.enterContext(unittest.mock.patch.object(ss, "stdin_is_tty", lambda: self.tty))
        self.enterContext(unittest.mock.patch.object(ss, "ask", self.answer))
        self.enterContext(unittest.mock.patch.object(ss.time, "sleep", lambda _seconds: None))

    def answer(self, question: str) -> bool:
        self.asked.append(question)
        return self.reply

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ss.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write(self, title: str, content: bytes = SECRET, mode: int = 0o600) -> Path:
        path = self.home.joinpath(*title.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, mode)
        return path

    def vault(self, bucket: str = "alpha") -> str:
        return self.fake.add_vault(f"Secrets {bucket}")

    def item(self, vid: str, title: str) -> dict[str, object]:
        hits = [it for it in self.fake.items[vid].values() if it["title"] == title]
        self.assertEqual(len(hits), 1, title)
        return hits[0]

    def assert_quiet(self, *texts: str) -> None:
        """No secret, no full digest, and no JSON reaches the output."""
        joined = "".join(texts)
        self.assertNotIn(SECRET.decode().strip(), joined)
        self.assertIsNone(re.search(r"[0-9a-f]{64}", joined))
        self.assertNotIn('{"', joined)


class TestSession(Case):
    def test_an_ambient_session_is_used_and_never_signed_out(self) -> None:
        code, _, _ = self.run_main("status")
        self.assertEqual(code, 0)
        self.assertNotIn(("signin",), self.fake.keys())
        self.assertNotIn(("signout",), self.fake.keys())

    def test_signin_passes_the_session_by_environment_and_signs_it_out(self) -> None:
        self.fake.ambient = False
        code, _, _ = self.run_main("status")
        self.assertEqual(code, 0)
        keys = self.fake.keys()
        self.assertEqual(keys[-1], ("signout",))
        signin = self.fake.calls[keys.index(("signin",))]
        self.assertTrue(signin[2], "signin runs attached to the terminal")
        after = self.fake.calls[keys.index(("signin",)) + 1 :]
        for argv, env, _ in after:
            self.assertEqual(env.get("OP_SESSION_alpha"), "constructed-token")
            self.assertNotIn("constructed-token", " ".join(argv))

    def test_a_session_it_created_is_signed_out_after_a_refusal(self) -> None:
        self.fake.ambient = False
        code, _, err = self.run_main("status", "--bucket", "absent")
        self.assertEqual(code, 1, err)
        self.assertEqual(self.fake.keys()[-1], ("signout",))

    def test_a_session_it_created_is_signed_out_on_an_interrupt(self) -> None:
        self.fake.ambient = False
        self.fake.raise_on[("vault", "list")] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_main("status")
        self.assertEqual(self.fake.keys()[-1], ("signout",))

    def test_signin_needs_a_terminal(self) -> None:
        self.fake.ambient = False
        self.tty = False
        code, _, err = self.run_main("status")
        self.assertEqual(code, 1)
        self.assertIn("terminal", err)
        self.assertNotIn(("signin",), self.fake.keys())

    def test_both_signin_forms_parse_and_the_name_is_read_not_assumed(self) -> None:
        for text, expected in (
            (b'export OP_SESSION_abc="tok-one"\n', ("OP_SESSION_abc", "tok-one")),
            (b'$env:OP_SESSION_xyz="tok-two"\r\n', ("OP_SESSION_xyz", "tok-two")),
            (b"# comment\nexport OTHER_NAME='tok-three'\n", ("OTHER_NAME", "tok-three")),
        ):
            with self.subTest(text=text):
                self.assertEqual(ss.parse_session(text), expected)
        for text in (b"", b"no assignment here\n"):
            with self.subTest(text=text):
                self.assertIsNone(ss.parse_session(text))
        for text in (b'export A="1"\nexport B="2"\n', b"\xff\xfe"):
            with self.subTest(text=text), self.assertRaises(ss.Execution):
                ss.parse_session(text)

    def test_more_than_one_account_needs_account(self) -> None:
        self.fake.accounts = 2
        code, _, err = self.run_main("status")
        self.assertEqual(code, 1)
        self.assertIn("--account", err)
        code, _, _ = self.run_main("status", "--account", "alpha")
        self.assertEqual(code, 0)
        for argv, _, _ in self.fake.calls[2:]:
            self.assertEqual(argv[-2:], ["--account", "alpha"])

    def test_no_configured_account_is_a_refusal(self) -> None:
        self.fake.accounts = 0
        self.assertEqual(self.run_main("status")[0], 1)

    def test_root_is_refused_before_any_call(self) -> None:
        with unittest.mock.patch.object(ss.os, "geteuid", lambda: 0, create=True):
            code, _, err = self.run_main("status")
        self.assertEqual(code, 1)
        self.assertIn("root", err)
        self.assertEqual(self.fake.calls, [])

    def test_a_write_needs_a_terminal_and_a_dry_run_does_not(self) -> None:
        self.write(".secrets/alpha_api_token")
        self.tty = False
        code, _, err = self.run_main(
            "push", "--bucket", "alpha", "--mode", "0600", ".secrets/alpha_api_token"
        )
        self.assertEqual(code, 1)
        self.assertIn("terminal", err)
        self.assertEqual(self.fake.calls, [])
        code, _, err = self.run_main(
            "push",
            "--bucket",
            "alpha",
            "--mode",
            "0600",
            "--dry-run",
            str(self.home / ".secrets/alpha_api_token"),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.writes(), [])

    def test_every_writing_subcommand_needs_a_terminal(self) -> None:
        self.tty = False
        for argv in (
            ("pull", "--bucket", "alpha", "--yes"),
            ("archive", "--bucket", "alpha", "--yes", str(self.home / ".secrets/alpha")),
            ("selftest",),
        ):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 1)
                self.assertIn("terminal", err)
        self.assertEqual(self.fake.calls, [])

    def test_an_expired_session_after_the_prompt_writes_nothing(self) -> None:
        self.write(".secrets/alpha_api_token")

        def expire(question: str) -> bool:
            self.fake.ambient = False
            return True

        with unittest.mock.patch.object(ss, "ask", expire):
            code, _, err = self.run_main(
                "push",
                "--bucket",
                "alpha",
                "--mode",
                "0600",
                str(self.home / ".secrets/alpha_api_token"),
            )
        self.assertEqual(code, 1)
        self.assertIn("session ended", err)
        self.assertEqual(self.fake.writes(), [])

    def test_a_session_whoami_refuses_is_signed_out_and_fails(self) -> None:
        self.fake.ambient = False
        self.fake.signin_token = "another-token"
        code, _, err = self.run_main("status")
        self.assertEqual(code, 2)
        self.assertIn("does not accept", err)
        self.assertEqual(self.fake.keys()[-1], ("signout",))

    def test_signin_output_that_does_not_parse_fails(self) -> None:
        self.fake.ambient = False
        self.fake.signin_output = b'export A="constructed-1"\nexport B="constructed-2"\n'
        code, _, err = self.run_main("status")
        self.assertEqual(code, 2)
        self.assertIn("session assignment", err)

    def test_a_failed_signout_is_reported(self) -> None:
        self.fake.ambient = False
        self.fake.fail_before.add(("signout",))
        code, _, err = self.run_main("status")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: op signout failed", err)

    def test_a_signin_the_desktop_app_holds_is_used_and_not_signed_out(self) -> None:
        self.fake.ambient = False
        self.fake.signin_output = b""
        self.fake.signin_ambient = True
        code, _, err = self.run_main("status")
        self.assertEqual(code, 0, err)
        self.assertNotIn(("signout",), self.fake.keys())

    def test_a_signin_printing_nothing_that_whoami_still_refuses_fails(self) -> None:
        self.fake.ambient = False
        self.fake.signin_output = b""
        code, _, err = self.run_main("status")
        self.assertEqual(code, 2)
        self.assertIn("no session assignment", err)
        self.assertNotIn(("signout",), self.fake.keys())

    def test_a_failed_signin_leaves_nothing_to_sign_out(self) -> None:
        self.fake.ambient = False
        self.fake.fail_before.add(("signin",))
        code, _, err = self.run_main("status")
        self.assertEqual(code, 2)
        self.assertIn("op signin failed", err)
        self.assertNotIn(("signout",), self.fake.keys())


class TestTitles(Case):
    def test_unsafe_titles_are_refused_alike_on_every_platform(self) -> None:
        for title in (
            "",
            "/etc/alpha",
            "a//b",
            "./alpha",
            "a/../b",
            "..",
            "a\\b",
            "C:/alpha",
            "a:b",
            "alpha.",
            "alpha ",
            "dir./alpha",
            "CON",
            "nul.txt",
            "a/com1/b",
            "LPT9.log",
            "alpha\x01",
            "alpha\x7f",
            "alpha\n",
        ):
            with self.subTest(title=title), self.assertRaises(ss.Refusal):
                ss.validate_title(title)
        for title in ("a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "CONIN$", "conout$.txt"):
            with self.subTest(title=title), self.assertRaises(ss.Refusal):
                ss.validate_title(title)
        for title in (".secrets/alpha_api_token", "a/b/c.d", "console", "com10", ".hidden"):
            with self.subTest(title=title):
                self.assertEqual(ss.validate_title(title), title)

    def test_an_unsafe_title_in_a_vault_is_refused(self) -> None:
        self.fake.add_item(self.vault(), "../escape", SECRET)
        code, _, err = self.run_main("status")
        self.assertEqual(code, 1)
        self.assertIn("component", err)

    def test_a_duplicated_title_is_refused(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        self.assertEqual(self.run_main("status")[0], 1)

    def test_titles_colliding_case_insensitively_are_refused(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/Alpha", SECRET)
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        code, _, err = self.run_main("status")
        self.assertEqual(code, 1)
        self.assertIn("case-insensitively", err)

    def test_one_title_in_two_selected_buckets_is_refused(self) -> None:
        self.fake.add_item(self.vault("alpha"), ".secrets/shared", SECRET)
        self.fake.add_item(self.vault("beta"), ".secrets/shared", SECRET)
        code, _, err = self.run_main("pull", "--bucket", "alpha", "--bucket", "beta")
        self.assertEqual(code, 1)
        self.assertIn("both", err)
        self.assertFalse((self.home / ".secrets").exists())

    def test_a_path_outside_the_home_is_refused(self) -> None:
        outside = self.scratch / "outside"
        outside.write_bytes(SECRET)
        code, _, err = self.run_main("push", "--bucket", "alpha", "--mode", "0600", str(outside))
        self.assertEqual(code, 1)
        self.assertIn("not under", err)

    def test_duplicate_vault_names_are_refused(self) -> None:
        self.vault()
        self.vault()
        for argv in (("status",), ("status", "--bucket", "alpha"), ("pull", "--bucket", "alpha")):
            with self.subTest(argv=argv):
                code, _, err = self.run_main(*argv)
                self.assertEqual(code, 1)
                self.assertIn("ambiguous", err)

    def test_a_vault_name_is_matched_exactly(self) -> None:
        self.fake.add_vault("Secrets alpha-old")
        self.fake.add_vault("secrets alpha")
        code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("no vault", err)

    def test_a_bad_bucket_name_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            ss.main(["status", "--bucket", "Alpha"])
        self.assertEqual(raised.exception.code, 1)

    def test_a_windows_junction_on_the_path_is_never_written_through(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        (self.home / ".secrets").mkdir()
        with unittest.mock.patch.object(Path, "is_junction", lambda path: path.name == ".secrets"):
            code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertEqual(list((self.home / ".secrets").iterdir()), [])

    def test_a_path_spelled_in_another_case_than_on_disk_is_refused(self) -> None:
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "token").write_bytes(SECRET)
        with self.assertRaises(ss.Refusal) as raised:
            ss.title_for(self.home, str(self.home / ".Secrets" / "token"))
        self.assertIn("differs in case", str(raised.exception))
        self.assertEqual(
            ss.title_for(self.home, str(self.home / ".secrets" / "token")), ".secrets/token"
        )

    def test_one_title_in_two_buckets_differing_only_in_case_is_refused(self) -> None:
        self.fake.add_item(self.vault("alpha"), ".secrets/Shared", SECRET)
        self.fake.add_item(self.vault("beta"), ".secrets/shared", SECRET)
        code, _, err = self.run_main("pull", "--bucket", "alpha", "--bucket", "beta")
        self.assertEqual(code, 1)
        self.assertIn("both", err)

    def test_duplicate_vaults_that_are_not_buckets_are_skipped_not_refused(self) -> None:
        self.fake.add_vault("Secrets Not_A_Bucket")
        self.fake.add_vault("Secrets Not_A_Bucket")
        code, out, err = self.run_main("status")
        self.assertEqual(code, 0, err)
        self.assertIn("not a bucket name", out)


@POSIX_ONLY
class TestPush(Case):
    def push(self, *argv: str) -> tuple[int, str, str]:
        return self.run_main("push", "--bucket", "alpha", *argv)

    def test_a_first_push_creates_the_vault_and_reads_the_item_back(self) -> None:
        path = self.write(".secrets/alpha_api_token", mode=0o640)
        code, out, err = self.push(str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(list(self.fake.vaults.values()), ["Secrets alpha"])
        vid = next(iter(self.fake.vaults))
        item = self.item(vid, ".secrets/alpha_api_token")
        self.assertEqual(item["content"], SECRET)
        self.assertEqual(item["tags"], ("mode:0640",))
        self.assertEqual(len(self.asked), 1)
        self.assertIn(("document", "get"), self.fake.keys())
        self.assert_quiet(out, err)

    def test_an_update_edits_in_place_and_replaces_only_the_mode_tag(self) -> None:
        vid = self.vault()
        iid = self.fake.add_item(
            vid, ".secrets/alpha_api_token", b"old\n", ("keep-me", "mode:0600")
        )
        self.write(".secrets/alpha_api_token", mode=0o600)
        os.chmod(self.home / ".secrets/alpha_api_token", 0o640)
        code, out, err = self.push()
        self.assertEqual(code, 0, err)
        self.assertEqual(list(self.fake.items[vid]), [iid])
        self.assertEqual(self.fake.items[vid][iid]["content"], SECRET)
        self.assertEqual(self.fake.items[vid][iid]["tags"], ("keep-me", "mode:0640"))
        edit = [argv for argv in self.fake.writes() if key_of(argv) == ("document", "edit")]
        self.assertEqual(edit[0][2], iid)
        self.assert_quiet(out, err)

    def test_no_paths_refreshes_only_members_that_differ(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/same", SECRET)
        self.fake.add_item(vid, ".secrets/changed", b"old\n")
        self.fake.add_item(vid, ".secrets/absent", b"old\n")
        self.write(".secrets/same")
        self.write(".secrets/changed")
        code, out, err = self.push()
        self.assertEqual(code, 0, err)
        written = {opt(argv, "--vault") + argv[2] for argv in self.fake.writes()}
        changed = next(
            i for i, it in self.fake.items[vid].items() if it["title"] == ".secrets/changed"
        )
        self.assertEqual(written, {vid + changed})
        self.assertIn("absent locally", out)

    def test_a_remote_copy_newer_than_the_file_is_skipped_unless_overwrite_newer(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", b"remote\n", updated="2099-01-01T00:00:00Z")
        self.write(".secrets/alpha")
        code, out, _ = self.push()
        self.assertEqual(code, 0)
        self.assertIn("--overwrite-newer", out)
        self.assertEqual(self.fake.writes(), [])
        code, _, err = self.push("--overwrite-newer")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.item(vid, ".secrets/alpha")["content"], SECRET)

    def test_only_a_differing_mode_is_a_retag(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", SECRET, ("mode:0600",))
        self.write(".secrets/alpha", mode=0o640)
        code, _, err = self.push()
        self.assertEqual(code, 0, err)
        self.assertEqual([key_of(a) for a in self.fake.writes()], [("item", "edit")])
        self.assertEqual(self.item(vid, ".secrets/alpha")["tags"], ("mode:0640",))

    def test_a_mode_change_against_a_newer_remote_tag_is_stale_too(self) -> None:
        vid = self.vault()
        self.fake.add_item(
            vid, ".secrets/alpha", SECRET, ("mode:0600",), updated="2099-01-01T00:00:00Z"
        )
        path = self.write(".secrets/alpha", mode=0o644)
        code, out, _ = self.push()
        self.assertEqual(code, 0)
        self.assertIn("--overwrite-newer", out)
        code, _, err = self.push(str(path))
        self.assertEqual(code, 1)
        self.assertIn("--overwrite-newer", err)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.item(vid, ".secrets/alpha")["tags"], ("mode:0600",))

    def test_a_malformed_tag_on_an_existing_item_needs_mode(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", b"old\n", ("mode:0600", "mode:0644"))
        self.write(".secrets/alpha")
        code, _, err = self.push()
        self.assertEqual(code, 1)
        self.assertIn("pass --mode", err)
        self.assertEqual(self.fake.writes(), [])
        code, _, err = self.push("--mode", "0600")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.item(vid, ".secrets/alpha")["tags"], ("mode:0600",))

    def test_a_symlink_source_is_refused(self) -> None:
        target = self.write("target")
        (self.home / "link").symlink_to(target)
        code, _, err = self.push(str(self.home / "link"))
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        self.assertEqual(self.fake.writes(), [])

    def test_an_empty_file_is_refused_unless_allow_empty(self) -> None:
        path = self.write(".secrets/empty", b"")
        self.assertEqual(self.push(str(path))[0], 1)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.push("--allow-empty", str(path))[0], 0)

    def test_a_carriage_return_in_text_warns(self) -> None:
        path = self.write(".secrets/crlf", b"alpha\r\n")
        code, _, err = self.push(str(path))
        self.assertEqual(code, 0)
        self.assertIn("carriage return", err)

    def test_a_create_that_reports_failure_but_landed_is_verified_not_retried(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.fail_after.add(("document", "create"))
        code, _, err = self.push(str(path))
        self.assertEqual(code, 0, err)
        creates = [a for a in self.fake.writes() if key_of(a) == ("document", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("is listed", err)

    def test_a_create_that_failed_is_not_retried(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.fail_before.add(("document", "create"))
        code, _, err = self.push(str(path))
        self.assertEqual(code, 2)
        creates = [a for a in self.fake.writes() if key_of(a) == ("document", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("constructed failure", err)

    def test_a_read_back_that_differs_fails(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.corrupt = True
        code, out, err = self.push(str(path))
        self.assertEqual(code, 2)
        self.assertIn("reads back", err)
        self.assert_quiet(out, err)

    def test_a_dry_run_prints_the_writes_and_sends_none(self) -> None:
        path = self.write(".secrets/alpha")
        code, out, err = self.push("--dry-run", str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.writes(), [])
        self.assertIn("would run: op vault create", out)
        self.assertIn("would run: op document create", out)
        self.assertEqual(self.asked, [])

    def test_a_dry_run_update_sends_nothing(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", b"old\n")
        self.write(".secrets/alpha")
        code, out, err = self.push("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would run: op document edit", out)
        self.assertIn("would run: op item edit", out)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(self.item(vid, ".secrets/alpha")["content"], b"old\n")

    def test_a_declined_prompt_writes_nothing(self) -> None:
        path = self.write(".secrets/alpha")
        self.reply = False
        self.assertEqual(self.push(str(path))[0], 1)
        self.assertEqual(self.fake.writes(), [])

    def test_yes_skips_the_prompt(self) -> None:
        path = self.write(".secrets/alpha")
        self.assertEqual(self.push("--yes", str(path))[0], 0)
        self.assertEqual(self.asked, [])

    def test_a_case_insensitive_collision_with_a_member_is_refused(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/Alpha", SECRET)
        path = self.write(".secrets/alpha")
        code, _, err = self.push(str(path))
        self.assertEqual(code, 1)
        self.assertIn("case-insensitively", err)

    def test_a_file_changed_after_the_plan_fails_its_read_back(self) -> None:
        path = self.write(".secrets/alpha")

        def change(question: str) -> bool:
            path.write_bytes(b"changed\n")
            return True

        with unittest.mock.patch.object(ss, "ask", change):
            code, _, err = self.push(str(path))
        self.assertEqual(code, 2)
        self.assertIn("where the plan read", err)
        vid = next(iter(self.fake.vaults))
        self.assertEqual(self.item(vid, ".secrets/alpha")["content"], b"changed\n")

    def test_a_vault_create_that_lists_nothing_fails(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.vault_create_noop = True
        code, _, err = self.push(str(path))
        self.assertEqual(code, 2)
        self.assertIn("not listed after op vault create", err)

    def test_an_edit_that_moves_the_item_to_another_id_fails(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", b"old\n")
        self.write(".secrets/alpha")
        self.fake.edit_moves_id = True
        code, _, err = self.push()
        self.assertEqual(code, 2)
        self.assertIn("different item", err)

    def test_tags_that_merge_rather_than_replace_fail(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", b"old\n", ("mode:0640",))
        self.write(".secrets/alpha")
        self.fake.merge_tags = True
        code, _, err = self.push()
        self.assertEqual(code, 2)
        self.assertIn("mode tags", err)

    def test_a_vault_create_that_reports_failure_but_landed_is_used(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.fail_after.add(("vault", "create"))
        code, _, err = self.push(str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.keys().count(("vault", "create")), 1)
        self.assertEqual(list(self.fake.vaults.values()), ["Secrets alpha"])

    def test_a_vault_create_that_failed_stops_the_push(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.fail_before.add(("vault", "create"))
        code, _, _ = self.push(str(path))
        self.assertEqual(code, 2)
        self.assertEqual([key_of(a) for a in self.fake.writes()], [("vault", "create")])

    def test_a_path_under_an_unlistable_directory_is_refused_by_name(self) -> None:
        locked = self.home / ".secrets"
        locked.mkdir()
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        code, _, err = self.push(str(locked / "alpha"))
        self.assertEqual(code, 1)
        self.assertIn("cannot be listed", err)

    def test_two_paths_differing_only_in_case_are_refused(self) -> None:
        upper = self.write(".secrets/Token")
        lower = self.write(".secrets/token")
        code, _, err = self.push(str(upper), str(lower))
        self.assertEqual(code, 1)
        self.assertIn("differ only in case", err)
        self.assertEqual(self.fake.writes(), [])

    def test_a_directory_at_a_title_is_refused(self) -> None:
        (self.home / ".secrets/alpha").mkdir(parents=True)
        code, _, err = self.push(str(self.home / ".secrets/alpha"))
        self.assertEqual(code, 1)
        self.assertIn("not a regular file", err)

    def test_a_named_path_that_does_not_exist_is_refused(self) -> None:
        code, _, err = self.push(str(self.home / ".secrets/absent"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertEqual(self.fake.writes(), [])

    def test_no_paths_and_no_vault_is_refused(self) -> None:
        code, _, err = self.push()
        self.assertEqual(code, 1)
        self.assertIn("name the files to push", err)

    def test_a_binary_file_with_a_carriage_return_does_not_warn(self) -> None:
        path = self.write(".secrets/binary", b"alpha\r\n\x00")
        code, _, err = self.push(str(path))
        self.assertEqual(code, 0, err)
        self.assertNotIn("carriage return", err)

    def test_a_non_utf8_file_with_a_carriage_return_does_not_warn(self) -> None:
        path = self.write(".secrets/binary", b"\xff\r\n")
        code, _, err = self.push(str(path))
        self.assertEqual(code, 0, err)
        self.assertNotIn("carriage return", err)

    def test_a_named_path_whose_remote_copy_is_newer_is_refused(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", b"remote\n", updated="2099-01-01T00:00:00Z")
        path = self.write(".secrets/alpha")
        code, _, err = self.push(str(path))
        self.assertEqual(code, 1)
        self.assertIn("--overwrite-newer", err)
        self.assertEqual(self.fake.writes(), [])

    def test_an_item_missing_after_the_write_fails(self) -> None:
        path = self.write(".secrets/alpha")
        self.fake.drop_create = True
        code, _, err = self.push(str(path))
        self.assertEqual(code, 2)
        self.assertIn("not listed after the write", err)

    def test_a_parent_that_is_a_file_is_refused_by_name(self) -> None:
        self.write(".secrets")
        code, _, err = self.push(str(self.home / ".secrets/alpha"))
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", err)


class TestWindowsModes(Case):
    """Windows behavior, reached by patching the platform switch, so it runs on every platform."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(unittest.mock.patch.object(ss, "IS_WINDOWS", True))

    def test_a_new_item_needs_mode(self) -> None:
        path = self.write(".secrets/alpha")
        code, _, err = self.run_main("push", "--bucket", "alpha", str(path))
        self.assertEqual(code, 1)
        self.assertIn("--mode", err)
        self.assertEqual(self.fake.writes(), [])
        code, _, err = self.run_main("push", "--bucket", "alpha", "--mode", "0640", str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(
            self.item(next(iter(self.fake.vaults)), ".secrets/alpha")["tags"], ("mode:0640",)
        )

    def test_an_update_keeps_the_existing_tag(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", b"old\n", ("mode:0640",))
        self.write(".secrets/alpha")
        code, _, err = self.run_main("push", "--bucket", "alpha")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.item(vid, ".secrets/alpha")["tags"], ("mode:0640",))

    def test_a_pull_ignores_the_tag(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET, ("mode:bogus",))
        code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.home / ".secrets/alpha").read_bytes(), SECRET)


@POSIX_ONLY
class TestPull(Case):
    def test_a_pull_writes_each_item_with_its_mode_and_new_parents_at_0700(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".config/alpha/deep/token", SECRET, ("mode:0640",))
        (self.home / ".config").mkdir(mode=0o755)
        os.chmod(self.home / ".config", 0o755)
        code, out, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 0, err)
        path = self.home / ".config/alpha/deep/token"
        self.assertEqual(path.read_bytes(), SECRET)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.home / ".config").stat().st_mode), 0o755)
        for made in (".config/alpha", ".config/alpha/deep"):
            self.assertEqual(stat.S_IMODE((self.home / made).stat().st_mode) & 0o077, 0)
        self.assertEqual(self.asked, [], "a create needs no prompt")
        self.assert_quiet(out, err)

    def test_a_differing_local_file_is_overwritten_only_on_confirmation(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET, updated="2099-01-01T00:00:00Z")
        path = self.write(".secrets/alpha", b"local\n")
        self.reply = False
        self.assertEqual(self.run_main("pull", "--bucket", "alpha")[0], 1)
        self.assertEqual(path.read_bytes(), b"local\n")
        self.reply = True
        self.assertEqual(self.run_main("pull", "--bucket", "alpha")[0], 0)
        self.assertEqual(path.read_bytes(), SECRET)
        self.assertEqual(len(self.asked), 2)

    def test_a_missing_or_malformed_mode_tag_is_refused(self) -> None:
        for tags in (
            (),
            ("mode:999",),
            ("mode:0600", "mode:0640"),
            ("mode:17777",),
            ("mode:",),
            ("mode:4755",),
            ("mode:1777",),
        ):
            with self.subTest(tags=tags):
                fake = FakeOp()
                fake.add_item(fake.add_vault("Secrets alpha"), ".secrets/alpha", SECRET, tags)
                with unittest.mock.patch.object(ss, "run_op", fake):
                    code, _, err = self.run_main("pull", "--bucket", "alpha")
                self.assertEqual(code, 1, err)
                self.assertIn("mode tag", err)
                self.assertFalse((self.home / ".secrets").exists())

    def test_a_symlink_on_the_path_is_never_written_through(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        elsewhere = self.scratch / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".secrets").symlink_to(elsewhere)
        code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_restore_home_must_be_mode_700(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        restore = self.scratch / "restore"
        restore.mkdir()
        os.chmod(restore, 0o755)
        code, _, err = self.run_main("pull", "--bucket", "alpha", "--home", str(restore))
        self.assertEqual(code, 1)
        self.assertIn("0700", err)
        os.chmod(restore, 0o700)
        code, _, err = self.run_main("pull", "--bucket", "alpha", "--home", str(restore))
        self.assertEqual(code, 0, err)
        self.assertEqual((restore / ".secrets/alpha").read_bytes(), SECRET)
        self.assertFalse((self.home / ".secrets").exists())

    def test_the_secret_lands_in_a_temporary_file_that_is_0600_before_its_mode_is_set(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET, ("mode:0644",))
        seen: list[int] = []
        real_fsync = os.fsync

        def observed(fd: int) -> None:
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            real_fsync(fd)

        with unittest.mock.patch.object(ss.os, "fsync", observed):
            code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [0o600])
        self.assertEqual(stat.S_IMODE((self.home / ".secrets/alpha").stat().st_mode), 0o644)

    def test_the_temporary_file_is_removed_when_the_rename_fails(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)

        def broken(_src: object, _dst: object) -> None:
            raise OSError("constructed rename failure")

        with unittest.mock.patch.object(ss.os, "replace", broken):
            code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 2)
        self.assertNotIn("constructed rename failure", err)
        self.assertEqual(list((self.home / ".secrets").iterdir()), [])

    def test_a_local_file_newer_than_the_remote_copy_is_skipped_unless_overwrite_newer(
        self,
    ) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET, updated="2000-01-01T00:00:00Z")
        path = self.write(".secrets/alpha", b"rotated\n")
        code, out, _ = self.run_main("pull", "--bucket", "alpha", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("--overwrite-newer", out)
        self.assertEqual(path.read_bytes(), b"rotated\n")
        code, _, err = self.run_main("pull", "--bucket", "alpha", "--yes", "--overwrite-newer")
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), SECRET)

    def test_a_dry_run_writes_nothing(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        code, out, _ = self.run_main("pull", "--bucket", "alpha", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would write", out)
        self.assertFalse((self.home / ".secrets").exists())

    def test_a_differing_mode_is_set_on_confirmation(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET, ("mode:0600",))
        path = self.write(".secrets/alpha", mode=0o644)
        self.assertEqual(self.run_main("pull", "--bucket", "alpha", "--yes")[0], 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_file_written_during_the_prompt_is_not_replaced(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        self.fake.add_item(vid, ".secrets/beta", SECRET, updated="2099-01-01T00:00:00Z")
        self.write(".secrets/beta", b"local\n")

        def race(question: str) -> bool:
            self.write(".secrets/alpha", b"raced\n")
            return True

        with unittest.mock.patch.object(ss, "ask", race):
            code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("changed locally", err)
        self.assertEqual((self.home / ".secrets/alpha").read_bytes(), b"raced\n")

    def test_a_restore_home_that_is_a_junction_is_refused(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        restore = self.scratch / "restore"
        restore.mkdir(mode=0o700)
        with unittest.mock.patch.object(Path, "is_junction", lambda path: path.name == "restore"):
            code, _, err = self.run_main("pull", "--bucket", "alpha", "--home", str(restore))
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)

    def test_a_restore_home_that_is_absent_or_a_file_is_refused(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        regular = self.scratch / "regular"
        regular.write_bytes(b"")
        for target in (self.scratch / "absent", regular):
            with self.subTest(target=target.name):
                code, _, err = self.run_main("pull", "--bucket", "alpha", "--home", str(target))
                self.assertEqual(code, 1)
                self.assertIn("--home", err)

    def test_a_directory_at_a_title_is_refused(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        (self.home / ".secrets/alpha").mkdir(parents=True)
        code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("not a regular file", err)

    def test_a_read_back_that_differs_from_what_was_written_fails(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)

        def short_write(path: Path, data: bytes, mode: int | None) -> None:
            path.write_bytes(data[:-1])

        with unittest.mock.patch.object(ss, "write_secret", short_write):
            code, _, err = self.run_main("pull", "--bucket", "alpha")
        self.assertEqual(code, 2)
        self.assertIn("reads back differently", err)


@POSIX_ONLY
class TestStatus(Case):
    def test_without_a_bucket_it_lists_titles_and_downloads_nothing(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/held", SECRET)
        self.fake.add_vault("Secrets Not_A_Bucket")
        self.write(".secrets/held")
        self.write(".secrets/unheld")
        code, out, err = self.run_main("status")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"present\s+\.secrets/held")
        self.assertIn("no bucket holds", out)
        self.assertIn(".secrets/unheld", out)
        self.assertIn("not a bucket name", out)
        self.assertNotIn(("document", "get"), self.fake.keys())

    def test_the_unheld_list_never_changes_the_exit_status(self) -> None:
        self.write(".secrets/unheld")
        self.assertEqual(self.run_main("status")[0], 0)
        os.chmod(self.home / ".secrets", 0o600)
        self.addCleanup(os.chmod, self.home / ".secrets", 0o700)
        code, _, err = self.run_main("status")
        self.assertEqual(code, 0, err)

    def test_with_a_bucket_it_compares_content_and_mode(self) -> None:
        vid = self.vault()
        for title, content in (
            ("same", SECRET),
            ("differs", b"remote\n"),
            ("only-remote", SECRET),
            ("mode", SECRET),
        ):
            self.fake.add_item(vid, f".secrets/{title}", content, ("mode:0600",))
        self.write(".secrets/same")
        self.write(".secrets/differs")
        self.write(".secrets/mode", mode=0o644)
        code, out, err = self.run_main("status", "--bucket", "alpha")
        self.assertEqual(code, 0, err)
        for state, title in (
            ("same", "same"),
            ("differs", "differs"),
            ("only-remote", "only-remote"),
            ("mode-differs", "mode"),
        ):
            self.assertRegex(out, rf"{state}\s+\.secrets/{title}\b")
        self.assertEqual(self.fake.writes(), [])
        self.assert_quiet(out, err)

    def test_status_never_reads_through_a_linked_parent(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        elsewhere = self.scratch / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "alpha").write_bytes(SECRET)
        (elsewhere / "unheld").write_bytes(SECRET)
        (self.home / ".secrets").symlink_to(elsewhere)
        code, _, err = self.run_main("status", "--bucket", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        code, out, _ = self.run_main("status")
        self.assertEqual(code, 0)
        self.assertNotIn("no bucket holds", out)
        self.assertRegex(out, r"link\s+\.secrets/alpha")


class TestArchive(Case):
    def test_archive_removes_one_item_by_its_listed_id(self) -> None:
        vid = self.vault()
        iid = self.fake.add_item(vid, ".secrets/alpha", SECRET)
        self.fake.add_item(vid, ".secrets/beta", SECRET)
        code, _, err = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.writes(), [["item", "delete", iid, "--vault", vid, "--archive"]])
        self.assertEqual([it["title"] for it in self.fake.items[vid].values()], [".secrets/beta"])
        self.assertEqual(len(self.asked), 1)

    def test_a_declined_archive_removes_nothing(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        self.reply = False
        code, _, _ = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(len(self.fake.items[vid]), 1)

    def test_a_dry_run_archive_sends_nothing(self) -> None:
        vid = self.vault()
        self.fake.add_item(vid, ".secrets/alpha", SECRET)
        code, out, err = self.run_main(
            "archive", "--bucket", "alpha", "--dry-run", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 0, err)
        self.assertIn("would run: op item delete", out)
        self.assertEqual(self.fake.writes(), [])
        self.assertEqual(len(self.fake.items[vid]), 1)

    def test_an_absent_vault_is_refused(self) -> None:
        code, _, err = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 1)
        self.assertIn("no vault", err)

    def test_an_absent_item_is_refused(self) -> None:
        self.vault()
        code, _, _ = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.writes(), [])

    def test_a_delete_that_reports_failure_but_landed_is_verified(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        self.fake.fail_after.add(("item", "delete"))
        code, _, err = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 0, err)
        self.assertIn("no longer listed", err)

    def test_a_delete_that_failed_is_reported(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        self.fake.fail_before.add(("item", "delete"))
        code, _, _ = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 2)

    def test_an_item_still_listed_after_archive_fails(self) -> None:
        self.fake.add_item(self.vault(), ".secrets/alpha", SECRET)
        self.fake.archive_noop = True
        code, _, err = self.run_main(
            "archive", "--bucket", "alpha", str(self.home / ".secrets/alpha")
        )
        self.assertEqual(code, 2)
        self.assertIn("still listed", err)


class TestSelftest(Case):
    def test_selftest_measures_in_a_scratch_vault_and_removes_it(self) -> None:
        temp_root = self.scratch / "tmp"
        temp_root.mkdir()
        with unittest.mock.patch.object(tempfile, "tempdir", str(temp_root)):
            code, out, err = self.run_main("selftest")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.fake.vaults, {})
        self.assertEqual(list(temp_root.iterdir()), [])
        self.assertNotIn("FAILS", out)
        for name in (
            "crlf",
            "nul-bytes",
            "item edit --tags replaces",
            "updated_at advances",
            "archived item",
        ):
            self.assertIn(name, out)
        self.assertEqual(len(self.asked), 1)

    def test_a_behavior_that_differs_is_reported_and_exits_1(self) -> None:
        self.fake.corrupt = True
        with unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)):
            code, out, _ = self.run_main("selftest")
        self.assertEqual(code, 1)
        self.assertIn("FAILS", out)
        self.assertEqual(self.fake.vaults, {})

    def test_a_missing_update_time_field_is_measured_before_it_stops_the_run(self) -> None:
        self.fake.omit_updated = True
        with unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)):
            code, out, _ = self.run_main("selftest")
        self.assertEqual(code, 2)
        self.assertRegex(out, r"FAILS\s+item list carries 'updated_at'")
        self.assertEqual(self.fake.vaults, {})

    def test_a_vault_create_that_lists_nothing_fails(self) -> None:
        self.fake.vault_create_noop = True
        with unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)):
            code, _, err = self.run_main("selftest")
        self.assertEqual(code, 2)
        self.assertIn("not listed after op vault create", err)

    def test_a_listing_that_fails_after_the_vault_create_says_to_check_by_hand(self) -> None:
        def flaky(argv: Sequence[str], env: Mapping[str, str], attached: bool) -> ss.Outcome:
            if key_of(argv) == ("vault", "list"):
                return ss.Outcome(0, b"not json", b"")
            return self.fake(argv, env, attached)

        with (
            unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)),
            unittest.mock.patch.object(ss, "run_op", flaky),
        ):
            code, _, err = self.run_main("selftest")
        self.assertEqual(code, 2)
        self.assertIn("delete it by hand if it exists", err)

    def test_results_print_even_when_the_scratch_removal_fails(self) -> None:
        def broken(_path: object) -> None:
            raise OSError("constructed removal failure")

        with (
            unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)),
            unittest.mock.patch.object(ss.shutil, "rmtree", broken),
        ):
            _, out, _ = self.run_main("selftest")
        self.assertIn("document round-trip, crlf", out)

    def test_a_failed_vault_delete_says_to_delete_it_by_hand(self) -> None:
        self.fake.fail_before.add(("vault", "delete"))
        with unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)):
            code, _, err = self.run_main("selftest")
        self.assertEqual(code, 0)
        self.assertIn("by hand", err)
        self.assertEqual(len(self.fake.vaults), 1)

    def test_the_vault_is_deleted_even_when_the_scratch_removal_fails(self) -> None:
        def broken(_path: object) -> None:
            raise OSError("constructed removal failure")

        with (
            unittest.mock.patch.object(tempfile, "tempdir", str(self.scratch)),
            unittest.mock.patch.object(ss.shutil, "rmtree", broken),
        ):
            code, _, _ = self.run_main("selftest")
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.vaults, {})


class TestOutput(Case):
    def test_an_unmodeled_error_prints_its_type_only(self) -> None:
        self.fake.raise_on[("vault", "list")] = ValueError("constructed-sentinel")
        code, _, err = self.run_main("status")
        self.assertEqual(code, 2)
        self.assertIn("ValueError", err)
        self.assertNotIn("constructed-sentinel", err)

    def test_a_listing_of_an_unexpected_shape_fails_loudly(self) -> None:
        for bad in (
            {"tags": "mode:0600"},
            {"updated_at": None},
            {"updated_at": "yesterday"},
            {"updated_at": "2020-01-01T00:00:00"},
        ):
            with self.subTest(bad=bad):
                fake = FakeOp()
                vid = fake.add_vault("Secrets alpha")
                fake.add_item(vid, ".secrets/alpha", SECRET)
                next(iter(fake.items[vid].values())).update(bad)
                with unittest.mock.patch.object(ss, "run_op", fake):
                    code, out, err = self.run_main("status")
                self.assertEqual(code, 2, err)
                self.assertNotIn('{"', out + err)

    def test_help_exits_zero_and_an_unknown_flag_is_refused(self) -> None:
        for argv, expected in (
            (["--help"], 0),
            (["-h"], 0),
            (["status", "--bogus-flag"], 1),
            ([], 1),
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                ss.main(argv)
            with self.subTest(argv=argv):
                self.assertEqual(raised.exception.code, expected)
        self.assertEqual(self.fake.calls, [])

    def test_a_failed_call_prints_its_stderr_capped(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ss.report_stderr(b"x" * (ss.STDERR_CAP + 500))
        self.assertTrue(err.getvalue().startswith("x" * ss.STDERR_CAP + " ... 500 more"))

    @POSIX_ONLY
    def test_op_missing_from_path_is_an_execution_failure(self) -> None:
        empty = self.scratch / "empty-bin"
        empty.mkdir()
        with self.assertRaises(ss.Execution) as raised:
            REAL_RUN_OP(["whoami"], {"PATH": str(empty)}, attached=False)
        self.assertIn("could not run op", str(raised.exception))

    @POSIX_ONLY
    def test_a_second_signal_during_cleanup_is_passed_over(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        cleaned: list[bool] = []

        def interrupted(argv: Sequence[str]) -> int:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
            finally:
                os.kill(os.getpid(), signal.SIGTERM)
                cleaned.append(True)
            return 0

        with (
            unittest.mock.patch.object(ss, "main", interrupted),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            outcome = ss.run([])
        self.assertEqual(outcome, signal.SIGTERM)
        self.assertEqual(cleaned, [True])

    def test_an_empty_value_is_never_read_as_absent(self) -> None:
        for argv in (
            ["pull", "--bucket", "alpha", "--home", ""],
            ["push", "--bucket", "", "alpha"],
            ["archive", "--bucket", "", "alpha"],
            ["push", "--bucket", "alpha", "--mode", "", "alpha"],
            ["status", "--account", ""],
        ):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                ss.main(argv)
            with self.subTest(argv=argv):
                self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.fake.calls, [])

    def test_mode_takes_an_octal_permission(self) -> None:
        for mode in ("999", "1777", "rw", "60"):
            with (
                self.subTest(mode=mode),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                ss.main(["push", "--bucket", "alpha", "--mode", mode, "alpha"])
            with self.subTest(mode=mode):
                self.assertEqual(raised.exception.code, 1)
        self.assertEqual(self.fake.calls, [])

    def test_each_op_output_shape_the_tool_assumes_fails_by_name(self) -> None:
        for key, raw, message in (
            (("account", "list"), b"{}", "not an array"),
            (("vault", "list"), b"{}", "not an array"),
            (("vault", "list"), b'[{"id": 1, "name": "Secrets alpha"}]', "without string fields"),
            (("item", "list"), b"not json", "not JSON"),
        ):
            with self.subTest(key=key, raw=raw):
                fake = FakeOp()
                fake.add_vault("Secrets alpha")
                fake.raw[key] = raw
                with unittest.mock.patch.object(ss, "run_op", fake):
                    code, _, err = self.run_main("status")
                self.assertEqual(code, 2, err)
                self.assertIn(message, err)


STUB = """#!/bin/sh
echo "$*" >>"$STUB_DIR/calls"
case "$1 $2" in
"account list") echo '[{}]' ;;
"vault list")
    if [ -f "$STUB_DIR/vault" ]; then printf '[{"id":"v1","name":"%s"}]\\n' "$(cat "$STUB_DIR/vault")"; else echo '[]'; fi ;;
"vault create") printf '%s' "$3" >"$STUB_DIR/vault" ;;
"vault delete") rm -f "$STUB_DIR/vault" ;;
"item list"|"document create") echo $$ >"$STUB_DIR/pid.tmp"; mv "$STUB_DIR/pid.tmp" "$STUB_DIR/pid"; exec sleep 30 ;;
*)
    case "$1" in
    whoami) [ -n "$OP_SESSION_stub" ] || [ -f "$STUB_DIR/ambient" ] || exit 1 ;;
    signin) echo 'export OP_SESSION_stub="constructed-token"' ;;
    signout) ;;
    *) exit 1 ;;
    esac ;;
esac
"""

DRIVER = """import importlib.util, sys
spec = importlib.util.spec_from_file_location("secrets_sync", {script!r})
module = importlib.util.module_from_spec(spec)
sys.modules["secrets_sync"] = module
spec.loader.exec_module(module)
module.stdin_is_tty = lambda: True
module.ask = lambda question: True
status = module.run(sys.argv[1:])
if isinstance(status, module.signal.Signals):
    module.reraise(status)
sys.exit(status)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return (
        Path(f"/proc/{pid}").exists()
        and "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    )


@unittest.skipIf(sys.platform == "win32", "Windows cannot deliver SIGINT or SIGTERM to a script")
class TestInterrupt(unittest.TestCase):
    """A signal mid-call stops the `op` child, signs out, removes temporary files, and ends the run by that signal."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.stub_dir = root / "stub"
        self.stub_dir.mkdir()
        bin_dir = root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "op"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.temp = root / "tmp"
        self.temp.mkdir()
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "HOME": str(self.home),
            "TMPDIR": str(self.temp),
            "STUB_DIR": str(self.stub_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def interrupted(self, signum: int, *argv: str) -> tuple[int, str, str]:
        process = subprocess.Popen(
            [sys.executable, "-c", DRIVER.format(script=str(SCRIPT)), *argv],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        pid_file = self.stub_dir / "pid"
        child = 0
        try:
            deadline = time.monotonic() + 20
            while not pid_file.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "the stub op never reached its long-running call")
            child = int(pid_file.read_text(encoding="utf-8"))
            process.send_signal(signum)
            out, err = process.communicate(timeout=20)
            survived = alive(child)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if child and alive(child):
                os.kill(child, signal.SIGKILL)
        self.assertFalse(survived, "the op child outlived the interrupt")
        return process.returncode, out, err

    def calls(self) -> list[str]:
        return (self.stub_dir / "calls").read_text(encoding="utf-8").splitlines()

    def test_sigterm_during_a_call_signs_out_the_session_it_created(self) -> None:
        (self.stub_dir / "vault").write_text("Secrets alpha", encoding="utf-8")
        code, _, err = self.interrupted(signal.SIGTERM, "pull", "--bucket", "alpha", "--yes")
        self.assertEqual(code, -signal.SIGTERM, err)
        self.assertNotIn("Traceback", err)
        self.assertIn("interrupted by SIGTERM", err)
        self.assertIn("signin", self.calls())
        self.assertEqual(self.calls()[-1], "signout")

    def test_sigint_during_selftest_removes_its_scratch_files_and_vault(self) -> None:
        (self.stub_dir / "ambient").touch()
        code, _, err = self.interrupted(signal.SIGINT, "selftest")
        self.assertEqual(code, -signal.SIGINT, err)
        self.assertIn("interrupted by SIGINT", err)
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertTrue(
            any(line.startswith("vault delete v1") for line in self.calls()), self.calls()
        )
        self.assertNotIn("signout", self.calls())


if __name__ == "__main__":
    unittest.main()
