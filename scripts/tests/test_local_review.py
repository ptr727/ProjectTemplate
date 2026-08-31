#!/usr/bin/env python3
"""Exercise local_review.py's content key, receipt handling, and exit codes against real git trees.

Every case builds a throwaway repository with real remote-tracking refs, because the whole
mechanism rests on git's own merge-base, index, and attribute behavior rather than on anything
this module could stub convincingly.

The fixture isolates git's global and system configuration. Without that, a host with commit
signing on cannot commit inside the fixture at all, and a host with a different `core.autocrlf`
answers the attribute cases differently than CI does.

Run as `python3 scripts/tests/test_local_review.py`, or under
`python3 -m unittest discover -s scripts/tests`.
"""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "local_review.py"
sys.path.insert(0, str(SCRIPT.parent))
import local_review


def run(cwd: Path, *args: str) -> str:
    """A checked git call for test setup, loud on failure so a broken fixture is never silent."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


class RepoCase(unittest.TestCase):
    """A throwaway repository with one base commit and origin/develop plus origin/main refs.

    The remote refs are planted with update-ref rather than by adding a real remote and fetching,
    so the fixture needs no network and no second repository on disk. origin/main exists so a case
    can record against one target and verify against another.
    """

    target = "develop"

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Anything a case needs outside the repository still lives under a managed directory.
        # Reaching for the repository's own parent would write into the shared system temp root.
        # The file then outlives the run, and two concurrent runs collide on one name.
        self.outside = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.isolate_git_config()
        run(self.tmp, "init", "--initial-branch=develop", ".")
        run(self.tmp, "config", "user.email", "test@example.invalid")
        run(self.tmp, "config", "user.name", "Test")
        run(self.tmp, "config", "commit.gpgsign", "false")
        (self.tmp / "base.txt").write_text("base\n", encoding="utf-8")
        run(self.tmp, "add", "base.txt")
        run(self.tmp, "commit", "-m", "base")
        head = run(self.tmp, "rev-parse", "HEAD").strip()
        run(self.tmp, "update-ref", "refs/remotes/origin/develop", head)
        run(self.tmp, "update-ref", "refs/remotes/origin/main", head)
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

    def digest(self, target: str | None = None) -> str:
        _, digest, _ = local_review.current_state(target or self.target, self.tmp)
        return digest

    def marks(self) -> dict[str, str]:
        return local_review.fingerprints(local_review.merge_base(self.target, self.tmp), self.tmp)

    def record(self, reviewer: str, findings: int | None = 0, target: str | None = None) -> dict:
        t = target or self.target
        base, digest, _ = local_review.current_state(t, self.tmp)
        return local_review.write_pass(self.tmp, t, base, digest, reviewer, findings)

    def commit_all(self, message: str) -> None:
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", message)

    def main_quiet(self, argv: list[str]) -> int:
        """Run the CLI with its diagnostics captured, for a case asserting only the exit code."""
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return local_review.main(argv)


class ContentKeyCase(RepoCase):
    def test_key_survives_the_commit_boundary(self) -> None:
        """The property the whole design rests on: review untracked work, commit it, same key."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        untracked = self.digest()
        run(self.tmp, "add", "new.py")
        self.assertEqual(self.digest(), untracked, "staging changed the key")
        run(self.tmp, "commit", "-m", "add new")
        self.assertEqual(self.digest(), untracked, "committing changed the key")

    def test_key_survives_the_commit_boundary_under_a_text_filter(self) -> None:
        """The same property where git rewrites content on the way into the index.

        A blob id computed by hashing raw working-tree bytes cannot equal the one git stores for a
        CRLF file under `text` attributes, so the two states never collapse and `git add` moves
        the key. This repository's own .gitattributes applies such a filter to every text file, so
        this is the configuration the engine actually runs under, not an exotic one.
        """
        (self.tmp / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
        run(self.tmp, "add", ".gitattributes")
        run(self.tmp, "commit", "-m", "attributes")
        (self.tmp / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
        # Without this the case proves nothing.
        # A typo in .gitattributes, or a git build ignoring it, leaves the filter inactive.
        # The assertions below would then pass for the trivial reason.
        raw = run(self.tmp, "hash-object", "--no-filters", "--", "crlf.txt").strip()
        filtered = run(self.tmp, "hash-object", "--", "crlf.txt").strip()
        self.assertNotEqual(raw, filtered, "the text filter is not active, so this proves nothing")
        before = self.digest()
        run(self.tmp, "add", "crlf.txt")
        self.assertEqual(self.digest(), before, "staging a CRLF file moved the key")
        run(self.tmp, "commit", "-m", "add crlf")
        self.assertEqual(self.digest(), before, "committing a CRLF file moved the key")

    def test_one_byte_changes_the_key(self) -> None:
        """A fix to a review finding has to invalidate the receipt, or the gate proves nothing."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        before = self.digest()
        (self.tmp / "new.py").write_text("print('y')\n", encoding="utf-8")
        self.assertNotEqual(self.digest(), before)

    def test_index_only_content_reaches_the_fingerprint(self) -> None:
        """Content staged that the working tree no longer holds still gets committed and pushed.

        The assertion is on the fingerprint holding two states rather than only on the digest
        moving. A digest-only assertion also passes when the path merely enters the changed set,
        which would leave the index side of the fingerprint unproven.
        """
        (self.tmp / "base.txt").write_text("staged\n", encoding="utf-8")
        run(self.tmp, "add", "base.txt")
        (self.tmp / "base.txt").write_text("base\n", encoding="utf-8")
        self.assertEqual(
            run(self.tmp, "diff", "--name-only", "HEAD"), "", "fixture is not the case"
        )
        mark = self.marks()["base.txt"]
        self.assertEqual(
            len(mark.split("|")), 2, f"index state absent from the fingerprint: {mark}"
        )

    def test_a_mode_change_changes_the_key(self) -> None:
        """A chmod is a real diff git records, so a receipt taken before it must not still cover.

        The file is already edited, so it is already in the changed-path set before the chmod.
        Otherwise the chmod would move the key merely by adding a path, and the case would pass
        without the mode ever reaching the fingerprint.
        """
        target = self.tmp / "base.txt"
        target.write_text("edited\n", encoding="utf-8")
        before = self.digest()
        self.assertIn("base.txt", self.marks())
        target.chmod(target.stat().st_mode | 0o111)
        if "100755" not in run(self.tmp, "diff", "--raw", "HEAD"):
            self.skipTest("this filesystem does not carry an executable bit git can see")
        self.assertNotEqual(self.digest(), before)

    def test_a_copy_and_a_move_do_not_share_a_key(self) -> None:
        """Comparing states against the merge base means a deletion is always visible.

        A diff listing with rename detection reports only a rename's destination, so turning a
        copy into a move would leave the source's deletion out of the key entirely.
        """
        run(self.tmp, "mv", "base.txt", "moved.txt")
        moved = self.digest()
        self.assertEqual(self.marks()["base.txt"], "w:absent")
        run(self.tmp, "reset", "--hard", "HEAD")
        (self.tmp / "moved.txt").write_text("base\n", encoding="utf-8")
        self.assertNotIn("base.txt", self.marks())
        self.assertNotEqual(moved, self.digest())

    def test_deleting_a_tracked_file_changes_the_key(self) -> None:
        before = self.digest()
        run(self.tmp, "rm", "-q", "base.txt")
        self.assertNotEqual(self.digest(), before)
        self.assertEqual(self.marks()["base.txt"], "w:absent")

    def test_an_unchanged_path_is_not_in_the_key(self) -> None:
        """Otherwise every file in the repository would be reviewed content."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(sorted(self.marks()), ["new.py"])

    def test_a_slashed_target_prefers_the_remote_over_a_local_branch(self) -> None:
        """A branch such as release/v1 is an ordinary name, not a fully qualified ref.

        Treating any slash as "already a ref" measures against the local branch of that name, and
        a local branch that has moved on then defines the review scope with no error at all, which
        is a silent wrong answer rather than a failure.
        """
        run(self.tmp, "update-ref", "refs/remotes/origin/release/v1", "HEAD")
        run(self.tmp, "checkout", "-b", "release/v1")
        (self.tmp / "moved.txt").write_text("local moved on\n", encoding="utf-8")
        run(self.tmp, "add", "moved.txt")
        run(self.tmp, "commit", "-m", "local only")
        run(self.tmp, "checkout", "task")
        remote = run(self.tmp, "rev-parse", "origin/release/v1").strip()
        local = run(self.tmp, "rev-parse", "release/v1").strip()
        self.assertNotEqual(remote, local, "fixture does not distinguish the two")
        self.assertEqual(local_review.target_ref("release/v1", self.tmp), "origin/release/v1")
        self.assertEqual(local_review.merge_base("release/v1", self.tmp), remote)

    def test_a_target_that_only_exists_on_another_remote_is_used_as_written(self) -> None:
        """This is what lets a fork-based flow name an upstream branch directly."""
        run(self.tmp, "update-ref", "refs/remotes/upstream/main", "HEAD")
        self.assertEqual(local_review.target_ref("upstream/main", self.tmp), "upstream/main")

    def test_a_target_resolving_nowhere_is_a_boundary(self) -> None:
        with self.assertRaises(local_review.CannotRun):
            local_review.target_ref("no-such-branch-anywhere", self.tmp)

    def test_the_origin_prefix_is_optional_when_matching_a_receipt(self) -> None:
        """A skill step and a hook spelling the target differently must not deadlock."""
        self.assertTrue(local_review.same_target("develop", "origin/develop"))
        self.assertTrue(local_review.same_target("origin/develop", "develop"))
        self.assertFalse(local_review.same_target("main", "upstream/main"))

    def test_current_state_uses_the_real_merge_base(self) -> None:
        """One half of the claim: the base in the key is the fork point git computes."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        base, _, _ = local_review.current_state(self.target, self.tmp)
        self.assertEqual(base, run(self.tmp, "merge-base", "origin/develop", "HEAD").strip())

    def test_the_digest_mixes_the_merge_base(self) -> None:
        """The other half: two different bases over one identical mark set differ.

        Asserting only that two targets give different digests is weaker than it looks, since the
        changed-path sets usually differ too and the digest would move on that alone.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        base, digest, _ = local_review.current_state(self.target, self.tmp)
        marks = local_review.fingerprints(base, self.tmp)
        other = local_review.content_digest("0" * 40, marks)
        self.assertNotEqual(digest, other)

    def test_a_symlink_is_keyed_as_a_link_not_as_its_pointee(self) -> None:
        """git stores a link as its target path, so the key must not follow it out of the tree."""
        outside = self.outside / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        try:
            (self.tmp / "link").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("this platform does not permit creating a symlink")
        mark = self.marks().get("link", "")
        if "120000:" not in mark:
            self.skipTest(f"this checkout does not store links as links: {mark}")
        before = self.digest()
        outside.write_text("different content entirely\n", encoding="utf-8")
        self.assertEqual(self.digest(), before, "the key followed the link out of the tree")

    def test_a_path_that_is_not_valid_utf8_is_keyed_under_its_real_name(self) -> None:
        """A name holding a raw high byte is what exercises the decoding, and a valid one is not.

        A merely non-ASCII name such as an accented one is valid UTF-8, so it round-trips under a
        strict decoder and proves nothing about the surrogateescape handling the engine relies on.
        """
        raw = b"docs/broken-\xff-name.md"
        (self.tmp / "docs").mkdir()
        try:
            (self.tmp / os.fsdecode(raw)).write_bytes(b"x\n")
        except (OSError, UnicodeError):
            self.skipTest("this filesystem does not accept a non-UTF-8 name")
        name = os.fsdecode(raw)
        self.assertIn(name, self.marks())
        run(self.tmp, "add", "-A")
        run(self.tmp, "commit", "-m", "add")
        self.assertIn(name, self.marks())

    def test_a_submodule_bump_is_a_state_not_a_crash(self) -> None:
        """A gitlink is a directory on disk, so reading it as a file would raise."""
        sub = self.outside / "sub"
        sub.mkdir()
        run(sub, "init", "--initial-branch=main", ".")
        run(sub, "config", "user.email", "test@example.invalid")
        run(sub, "config", "user.name", "Test")
        run(sub, "config", "commit.gpgsign", "false")
        (sub / "f.txt").write_text("x\n", encoding="utf-8")
        run(sub, "add", "f.txt")
        run(sub, "commit", "-m", "sub")
        added = subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "sub"],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode != 0:
            self.skipTest(f"submodules unavailable here: {added.stderr.strip()}")
        before = self.digest()
        self.assertIn("160000:", self.marks()["sub"])
        # Adding a submodule is not bumping one, and only the bump exercises the gitlink state.
        (sub / "f.txt").write_text("moved on\n", encoding="utf-8")
        run(sub, "commit", "-am", "advance")
        # The same file-protocol allowance the add above needed.
        # Without it this call fails outright on a git that disables the protocol by default.
        run(
            self.tmp / "sub",
            "-c",
            "protocol.file.allow=always",
            "pull",
            "--ff-only",
            str(sub),
            "main",
        )
        self.assertNotEqual(self.digest(), before, "a submodule bump did not move the key")

    def test_staging_the_worktree_writes_nothing_into_the_object_database(self) -> None:
        """Reading the tree stages it, and staging writes blobs, so the writes are redirected.

        Without the redirection this read permanently deposits the content of every unignored
        untracked file into the repository, a stray secret among them, from a command whose whole
        contract is that it only looks.
        """
        (self.tmp / ".env").write_text("TOKEN=constructed-not-real\n", encoding="utf-8")
        objects = local_review.git_dir(self.tmp) / "objects"

        # Relative paths rather than bare names.
        # Two loose objects in different two-hex directories can share the same 38-hex filename.
        # A name-only snapshot can therefore match while new objects were in fact written.
        def snapshot() -> list[str]:
            return sorted(
                q.relative_to(objects).as_posix() for q in objects.rglob("*") if q.is_file()
            )

        before = snapshot()
        states = local_review.worktree_states(self.tmp)
        after = snapshot()
        self.assertEqual(before, after, "staging leaked objects into the real database")
        oid = next(iter(states[".env"])).split(":")[-1]
        probe = subprocess.run(
            ["git", "cat-file", "-e", oid], cwd=str(self.tmp), capture_output=True, check=False
        )
        self.assertNotEqual(probe.returncode, 0, "the untracked file is readable from the repo")

    def test_the_object_store_resolves_inside_a_linked_worktree(self) -> None:
        """The fleet runs every task in a linked worktree, whose git directory holds no objects.

        The store lives in the common directory there, so joining `objects` onto the worktree's own
        git directory names a path that does not exist and attaches nothing as an alternate. The
        containment case above cannot catch this: its fixture is a primary checkout, where the
        joined path happens to be right.
        """
        linked = self.outside / "linked-tree"
        run(self.tmp, "worktree", "add", "-b", "linked-task", str(linked))
        self.addCleanup(run, self.tmp, "worktree", "remove", "--force", str(linked))
        self.assertFalse(
            (local_review.git_dir(linked) / "objects").exists(),
            "fixture is not the case: this worktree has its own objects directory",
        )
        store = local_review.objects_dir(linked)
        self.assertTrue(store.is_dir(), f"the object store did not resolve: {store}")
        # And the read still works from inside it, which a broken alternate would not guarantee.
        (linked / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertIn("new.py", local_review.worktree_states(linked))

    def test_staging_leaves_no_throwaway_paths_behind(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        local_review.worktree_states(self.tmp)
        gd = local_review.git_dir(self.tmp)
        leftovers = [p.name for p in gd.iterdir() if p.name.startswith("local-review-")]
        self.assertEqual(leftovers, [])

    def test_two_readers_do_not_share_a_throwaway_name(self) -> None:
        """A fixed name lets one invocation delete another's index mid-read.

        `git ls-files` against a missing index exits 0 with empty output, so the loser computes a
        confident wrong digest rather than failing.
        """
        gd = local_review.git_dir(self.tmp)
        seen = set()
        real_git = local_review.git

        def capture(*args: str, **kwargs: object) -> str:
            env = kwargs.get("env")
            if isinstance(env, dict) and "GIT_INDEX_FILE" in env:
                seen.add(env["GIT_INDEX_FILE"])
            return real_git(*args, **kwargs)  # type: ignore[arg-type]

        # Suppressing assignment here: the wrapper's signature is intentionally wider.
        local_review.git = capture  # type: ignore[assignment]
        self.addCleanup(setattr, local_review, "git", real_git)
        local_review.worktree_states(self.tmp)
        local_review.worktree_states(self.tmp)
        self.assertEqual(len(seen), 2, f"both reads used the same index path: {seen}")
        self.assertTrue(all(str(gd) in p for p in seen))

    def test_an_inherited_index_redirect_is_ignored(self) -> None:
        """A git hook runs with GIT_INDEX_FILE set, and a hook is the intended capture point.

        Inheriting it would make the real-index read answer for whatever index the caller pointed
        at, which during a partial commit is a pending one holding different content.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        expected = self.digest()
        decoy = local_review.git_dir(self.tmp) / "decoy-index"
        decoy.write_bytes(b"")
        prev = os.environ.get("GIT_INDEX_FILE")
        os.environ["GIT_INDEX_FILE"] = str(decoy)
        self.addCleanup(self.restore_env, "GIT_INDEX_FILE", prev)
        self.assertEqual(self.digest(), expected, "the inherited index redirect was honored")

    def test_staging_over_a_worktree_is_told_from_the_reverse(self) -> None:
        """A plain union of the two sides renders these identically, and they push different bytes."""
        f = self.tmp / "base.txt"
        f.write_text("A\n", encoding="utf-8")
        run(self.tmp, "add", "base.txt")
        f.write_text("B\n", encoding="utf-8")
        a_over_b = self.digest()
        run(self.tmp, "reset", "--hard", "HEAD")
        f.write_text("B\n", encoding="utf-8")
        run(self.tmp, "add", "base.txt")
        f.write_text("A\n", encoding="utf-8")
        self.assertNotEqual(self.digest(), a_over_b, "the two sides collapsed into one state")

    def test_the_engine_is_correct_from_a_subdirectory(self) -> None:
        """git reports paths relative to different places depending on the subcommand.

        An engine that inherited the process's own directory would key untracked files under names
        that do not resolve, and drop every one outside that directory.
        """
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "inner.py").write_text("print('i')\n", encoding="utf-8")
        (self.tmp / "top.py").write_text("print('t')\n", encoding="utf-8")
        from_root = self.digest()
        os.chdir(self.tmp / "sub")
        self.assertEqual(local_review.repo_root(), self.tmp)
        self.assertEqual(self.digest(), from_root, "the key changed with the working directory")


class HeadContentCase(RepoCase):
    """Content the pushed commit carries that the index and working tree no longer show.

    The index and the working tree describe what is about to be committed, never what is already
    committed, so a key built from those two alone stops seeing a path the moment the tree agrees
    with the merge base again. The commit still carries it, and a push still delivers it.
    """

    def test_a_committed_file_removed_in_the_tree_stays_in_the_key(self) -> None:
        (self.tmp / "payload.txt").write_text("secret\n", encoding="utf-8")
        self.commit_all("add payload")
        run(self.tmp, "rm", "-f", "payload.txt")
        # Index, working tree, and merge base now all agree the path is absent.
        # The commit that a push would deliver still holds it, which is what has to keep it in.
        self.assertIn("payload.txt", self.marks())
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_committed_edit_reverted_in_the_tree_invalidates_the_pass(self) -> None:
        (self.tmp / "base.txt").write_text("changed\n", encoding="utf-8")
        self.commit_all("edit base")
        self.record("agent-skill")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)
        # The commit keeps the edit, so the reviewed content is still what a push delivers.
        # Restoring the file from the merge base is what a key blind to HEAD reads as no change.
        run(self.tmp, "checkout", self.target, "--", "base.txt")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_path_held_by_head_alone_leaves_the_set_when_the_undo_is_committed(self) -> None:
        """The boundary the HEAD read actually moved, asserted rather than assumed harmless.

        A path in the set only because HEAD disagrees with the base is one the branch committed and
        the tree has since put back. Committing that undo drops it, and the key moves. That is the
        key following what a push delivers rather than the commit-boundary property breaking: before
        the commit a push delivers the change, and after it a push delivers the undo.
        """
        (self.tmp / "base.txt").write_text("changed\n", encoding="utf-8")
        self.commit_all("edit base")
        run(self.tmp, "checkout", self.target, "--", "base.txt")
        held_by_head = self.marks()
        self.assertIn("base.txt", held_by_head, "HEAD is no longer deciding membership")
        before = self.digest()
        self.commit_all("commit the undo")
        self.assertNotIn("base.txt", self.marks())
        self.assertNotEqual(self.digest(), before, "the pushed content changed and the key did not")

    def test_the_commit_itself_still_leaves_the_key_alone(self) -> None:
        """The half of the property that must survive, next to the half above that moved.

        Staged first, so the only thing between the two digests is the commit, which is the one
        operation the HEAD read newly sees. Staging is left out on purpose: `git add` on a modified
        tracked file collapses a mark that named the index separately, which moves the key by the
        existing design `state_mark` documents and would hide what this case is measuring.

        It is a floor rather than a proof of the HEAD read, and it passes with that read reverted,
        since both paths are in the set through their working-tree state either way.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        (self.tmp / "base.txt").write_text("edited\n", encoding="utf-8")
        run(self.tmp, "add", "-A")
        before = self.digest()
        run(self.tmp, "commit", "-m", "commit the reviewed work")
        self.assertEqual(self.digest(), before)


class StagingIsolationCase(RepoCase):
    def test_reading_the_worktree_does_not_touch_the_real_index(self) -> None:
        """The read stages the tree into a throwaway index, which must stay throwaway."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        before = run(self.tmp, "ls-files", "-s")
        local_review.worktree_states(self.tmp)
        self.assertEqual(run(self.tmp, "ls-files", "-s"), before, "the real index was modified")
        self.assertEqual(run(self.tmp, "status", "--porcelain"), "?? new.py\n")

    def test_the_staging_index_leaves_nothing_behind(self) -> None:
        """Left in the working tree it would stage itself, and its lock, as untracked content."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        local_review.worktree_states(self.tmp)
        gd = local_review.git_dir(self.tmp)
        leftovers = [p.name for p in gd.iterdir() if p.name.startswith("local-review-index")]
        self.assertEqual(leftovers, [])
        self.assertNotIn("local-review", run(self.tmp, "status", "--porcelain"))


class ReceiptCase(RepoCase):
    def test_check_reports_not_covered_then_covered(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)
        self.record("agent-skill")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)

    def test_an_empty_change_set_needs_no_pass(self) -> None:
        """A branch with no net content against its target has nothing for a review to read.

        Gating it would demand a review of an empty diff, and a receipt against that empty digest
        would attest to nothing. `status` still reports what is recorded, which is nothing, so the
        two answers must be read as the different questions they are.
        """
        self.assertEqual(len(self.marks()), 0, "the fixture branch already carries content")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(local_review.main(["status", "--target", self.target]), 0)
        reported = json.loads(out.getvalue())
        self.assertEqual(reported["changedPaths"], 0)
        self.assertFalse(reported["covered"], "status counts recorded passes, and none was")

    def test_an_unreadable_receipt_reports_the_boundary_even_with_nothing_to_gate(self) -> None:
        """A receipt that cannot be read off disk is the check not running, whatever the change set.

        The empty-change answer is a verdict, so reaching it without having tried to read the
        receipt would convert an execution boundary into a cheerful covered, which is the one
        reading a gate must never take.
        """
        self.assertEqual(len(self.marks()), 0, "the fixture branch already carries content")
        path = local_review.receipt_path(self.tmp)
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o000)
        self.addCleanup(path.unlink)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest("this user reads a mode 000 file, so the boundary cannot be provoked")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 2)

    def test_the_empty_change_set_exemption_is_the_only_reason_that_push_passes(self) -> None:
        """Proves the case above by the one byte that separates it from a gated branch.

        Without this, a branch that happened to carry no content would pass for the same reason a
        reviewed one does, and the exemption would be untested where it actually fires.
        """
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_stale_pass_is_dropped_rather_than_carried(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill")
        (self.tmp / "new.py").write_text("print('y')\n", encoding="utf-8")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)
        out = self.record("coderabbit-cli")
        self.assertEqual([p["reviewer"] for p in out["passes"]], ["coderabbit-cli"])

    def test_backends_accumulate_on_one_unchanged_diff(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", 3)
        out = self.record("coderabbit-cli", 1)
        self.assertEqual(
            sorted(p["reviewer"] for p in out["passes"]), ["agent-skill", "coderabbit-cli"]
        )

    def test_recording_the_same_backend_twice_replaces_its_pass(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", 3)
        out = self.record("agent-skill", 0)
        self.assertEqual(len(out["passes"]), 1)
        self.assertEqual(out["passes"][0]["findings"], 0)

    def test_no_subcommand_takes_its_target_from_the_receipt(self) -> None:
        """Reading the target out of the receipt and comparing against it is self-fulfilling.

        A branch retargeted after a pass was recorded would otherwise verify against the branch it
        used to target and pass, never having been reviewed against the diff being pushed.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", target="main")
        self.assertEqual(local_review.resolve_target(None), "develop")
        self.assertEqual(self.main_quiet(["check"]), 1)

    def test_the_same_target_named_two_ways_still_covers(self) -> None:
        """A skill step and a hook that spell the target differently must not deadlock."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", target="develop")
        self.assertEqual(self.main_quiet(["check", "--target", "origin/develop"]), 0)

    def test_a_receipt_for_another_target_does_not_cover_this_one(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", target="main")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_hand_edited_receipt_gives_a_verdict_not_a_traceback(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill")
        local_review.receipt_path(self.tmp).write_text(
            '{"receiptVersion": "one"}', encoding="utf-8"
        )
        receipt, problems = local_review.read_receipt(self.tmp)
        self.assertIsNone(receipt)
        self.assertTrue(problems)
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_an_unparseable_receipt_gives_a_verdict_not_a_traceback(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        local_review.receipt_path(self.tmp).write_text("{not json", encoding="utf-8")
        receipt, problems = local_review.read_receipt(self.tmp)
        self.assertIsNone(receipt)
        self.assertTrue(problems)
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_non_utf8_receipt_is_a_verdict_not_a_boundary(self) -> None:
        """Unusable content is a real answer about coverage, unlike a file that cannot be read.

        Decoding inside the block guarding the disk read raises UnicodeDecodeError past both
        handlers, so the boundary code is reported for a receipt that was read perfectly well.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        local_review.receipt_path(self.tmp).write_bytes(b"\xff\xfe not utf-8 at all")
        receipt, problems = local_review.read_receipt(self.tmp)
        self.assertIsNone(receipt)
        self.assertTrue(any("parse" in p for p in problems), problems)
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_malformed_pass_entry_is_caught_before_it_is_formatted(self) -> None:
        """A non-string reviewer name would otherwise reach a formatting line and raise."""
        problems = local_review.receipt_problems(
            {
                "receiptVersion": local_review.RECEIPT_VERSION,
                "target": "develop",
                "mergeBase": "x",
                "contentDigest": "y",
                "passes": [{"reviewer": 1}],
            }
        )
        self.assertTrue(any("reviewer" in p for p in problems), problems)

    def test_an_unreadable_receipt_file_is_a_boundary_not_a_verdict(self) -> None:
        """The tool's own state file being unreadable means this check never ran."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill")
        path = local_review.receipt_path(self.tmp)
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest("this process can read a mode-000 file, so the case cannot be built")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 2)

    def test_record_refuses_content_that_moved_since_the_review(self) -> None:
        """The load-bearing path: a pass must attest to what a reviewer actually saw.

        Nothing binds the moment the review finished to the moment it is recorded, so a
        format-on-save or a hook autofix in between would otherwise be stamped as reviewed.
        """
        (self.tmp / "a.py").write_text("reviewed content\n", encoding="utf-8")
        reviewed = self.digest()
        (self.tmp / "a.py").write_text("something else entirely\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(["record", "--reviewer", "agent-skill", "--expect-digest", reviewed]),
            2,
        )
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_receipt_naming_another_target_gets_no_paste_ready_remedy(self) -> None:
        """The remedy is withheld precisely where following it would stamp unread content.

        A pass recorded against one branch and a check measuring another disagree about scope. The
        ordinary remedy line records this check's scope, which runs, succeeds, replaces the
        correctly scoped receipt, and passes the next check over a diff no reviewer read.
        """
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", target="main")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_review.main(["check", "--target", self.target]), 1)
        printed = err.getvalue()
        self.assertNotIn("local_review.py record", printed, "the unsafe remedy was offered anyway")
        self.assertIn("wrong scope", printed)

    def test_the_remedy_still_appears_when_the_targets_agree(self) -> None:
        """The floor under the case above, so withholding cannot quietly become withholding always."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill")
        (self.tmp / "new.py").write_text("print('y')\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_review.main(["check", "--target", self.target]), 1)
        self.assertIn("local_review.py record", err.getvalue())

    def test_the_check_failure_prints_a_command_that_actually_runs(self) -> None:
        """The remedy line is the one actionable thing the failure emits.

        It has to carry every required flag and the real digest, or following it lands the reader
        on a usage error instead of a recorded pass.
        """
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_review.main(["check", "--target", self.target]), 1)
        printed = err.getvalue()
        digest = self.digest()
        self.assertIn("--expect-digest", printed)
        self.assertIn(digest, printed, "the remedy carries an abbreviated digest, so it will fail")
        # Run the printed command verbatim.
        # Repairing it here, by dropping or filling a placeholder, would hide the defect.
        line = next(l for l in printed.splitlines() if "local_review.py record" in l)
        argv = line.split()[2:]
        self.assertNotIn("<n>", argv, "the printed command carries a placeholder, so it cannot run")
        self.assertEqual(self.main_quiet(argv), 0, f"the printed command failed: {argv}")
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)

    def test_the_remedy_quotes_a_target_holding_shell_metacharacters(self) -> None:
        """git permits them in a ref name, confirmed for `;`, `$(...)`, `&`, and a quote.

        Interpolating one raw into a line the reader is invited to paste would let a branch name
        run commands, so the printed value has to survive a shell round trip as one argument.
        """
        name = "feat;echo-pwned"
        run(self.tmp, "branch", name)
        run(self.tmp, "update-ref", f"refs/remotes/origin/{name}", "HEAD")
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local_review.main(["check", "--target", name]), 1)
        line = next(l for l in err.getvalue().splitlines() if "local_review.py record" in l)
        # The decisive assertion is that the quoted form is what was printed.
        # Splitting with shlex alone is not enough, since it does not treat `;` as an operator.
        # The raw name survives as one token there whether or not it was quoted.
        self.assertIn(shlex.quote(name), line, f"the target was interpolated raw: {line}")
        self.assertIn(name, shlex.split(line), "the target did not survive as one argument")

    def run_piped(self, args: list[str], shell: str) -> int:
        """Run the CLI for real with its output piped into a reader that exits first.

        Driven through a shell rather than in process, because the failure being tested is a
        genuine EPIPE on a descriptor, and the exit code being tested includes the one the
        interpreter produces during its own shutdown flush, which no in-process case can reach.
        """
        script = f"python3 {SCRIPT} {' '.join(args)} {shell}"
        proc = subprocess.run(
            ["bash", "-c", f"{script}; exit ${{PIPESTATUS[0]}}"],
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode

    def test_a_closed_stdout_reader_does_not_break_the_exit_contract(self) -> None:
        """`status | head -1` has to stay inside 0, 1, 2 rather than exiting 120."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_piped(["status", "--target", self.target], "| head -1"), 0)

    def test_a_reader_that_takes_nothing_still_reports_the_verdict(self) -> None:
        """The work completed, so a reader exiting before any of it arrived is not a failure."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_piped(["status", "--target", self.target], "| head -0"), 0)

    def test_a_closed_stderr_reader_still_reports_not_covered(self) -> None:
        """The case that rules out reporting a blanket success when no verdict was returned yet.

        `check` writes its diagnostics to stderr before returning, so a pipe closing there would
        leave the verdict unreturned. Treating that as success reports covered for a branch that
        is not, which is the one wrong answer a gate must never give.
        """
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_piped(["check", "--target", self.target], "2>&1 | head -0"), 1)

    def test_record_requires_the_digest_the_review_saw(self) -> None:
        """An optional guard is the one a caller in a hurry omits.

        The omission would then look exactly like a pass that was properly bound to its review.
        """
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            local_review.main(["record", "--reviewer", "agent-skill"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_a_negative_finding_count_is_refused(self) -> None:
        """It would otherwise land in the receipt and mean nothing to whoever reads it."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(
                [
                    "record",
                    "--reviewer",
                    "agent-skill",
                    "--findings",
                    "-1",
                    "--expect-digest",
                    self.digest(),
                ]
            ),
            2,
        )

    def test_record_accepts_the_digest_the_review_saw(self) -> None:
        (self.tmp / "a.py").write_text("reviewed content\n", encoding="utf-8")
        reviewed = self.digest()
        self.assertEqual(
            self.main_quiet(["record", "--reviewer", "agent-skill", "--expect-digest", reviewed]),
            0,
        )

    def test_record_will_not_forge_a_headless_backend(self) -> None:
        """Recording one by hand bypasses the completion check that makes its pass mean anything."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(["record", "--reviewer", "coderabbit-cli", "--expect-digest", "x"]), 2
        )

    def test_concurrent_records_do_not_drop_a_pass(self) -> None:
        """Accumulation is a read, a merge, and a replace, so it needs more than an atomic write."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        base, digest, _ = local_review.current_state(self.target, self.tmp)
        real = local_review._write_pass_locked

        def slow(*args: object, **kwargs: object) -> dict:
            # Widen the read-modify-write window the lock exists to close.
            time.sleep(0.2)
            # Suppressing arg-type here: the captured original is typed by its own signature.
            # This deliberately generic forwarding wrapper cannot restate that signature.
            return real(*args, **kwargs)  # type: ignore[arg-type]

        # Suppressing assignment here: rebinding a module function to a stand-in is the case.
        # The stand-in cannot match the original's exact signature.
        local_review._write_pass_locked = slow  # type: ignore[assignment]
        self.addCleanup(setattr, local_review, "_write_pass_locked", real)
        done: list[dict] = []
        threads = [
            threading.Thread(
                target=lambda r=r: done.append(
                    local_review.write_pass(self.tmp, self.target, base, digest, r, 0)
                )
            )
            for r in ("agent-skill", "coderabbit-cli")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        receipt, problems = local_review.read_receipt(self.tmp)
        self.assertEqual(problems, [])
        assert receipt is not None
        self.assertEqual(
            sorted(p["reviewer"] for p in receipt["passes"]),
            ["agent-skill", "coderabbit-cli"],
            "a concurrent record dropped the other pass",
        )

    def test_a_stale_lock_does_not_wedge_the_tool(self) -> None:
        """A process killed while holding it would otherwise block every later record forever."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        lock = Path(str(local_review.receipt_path(self.tmp)) + ".lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - local_review.STALE_LOCK_SECONDS - 60
        os.utime(lock, (old, old))
        base, digest, _ = local_review.current_state(self.target, self.tmp)
        out = local_review.write_pass(self.tmp, self.target, base, digest, "agent-skill", 0)
        self.assertEqual([p["reviewer"] for p in out["passes"]], ["agent-skill"])

    def test_breaking_a_stale_lock_does_not_remove_a_fresh_one(self) -> None:
        """Checking staleness and then unlinking is a race the breaker lock exists to close.

        The observing process can be descheduled between the two, the holder can release, and
        another process can take a fresh lock, which the unlink would then remove.
        """
        lock = Path(str(local_review.receipt_path(self.tmp)) + ".lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - local_review.STALE_LOCK_SECONDS - 60
        os.utime(lock, (old, old))
        real_unlink = Path.unlink

        def racing_stat(self_path: Path) -> os.stat_result:
            # Stand in for the holder releasing and a fresh lock being taken in the window.
            if self_path == lock:
                os.utime(lock, None)
            return os.stat(self_path)

        with unittest.mock.patch.object(Path, "stat", racing_stat):
            broke = local_review.break_stale_lock(lock)
        self.assertFalse(broke, "a lock that became fresh was still broken")
        self.assertTrue(lock.exists(), "the fresh lock was removed")
        real_unlink(lock)

    def test_the_breaker_leaves_nothing_behind(self) -> None:
        lock = Path(str(local_review.receipt_path(self.tmp)) + ".lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - local_review.STALE_LOCK_SECONDS - 60
        os.utime(lock, (old, old))
        self.assertTrue(local_review.break_stale_lock(lock))
        self.assertFalse(Path(str(lock) + ".break").exists())
        self.assertFalse(lock.exists())

    def test_a_breaker_already_held_defers_rather_than_racing(self) -> None:
        lock = Path(str(local_review.receipt_path(self.tmp)) + ".lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - local_review.STALE_LOCK_SECONDS - 60
        os.utime(lock, (old, old))
        breaker = Path(str(lock) + ".break")
        breaker.write_text("", encoding="utf-8")
        self.addCleanup(breaker.unlink, True)
        self.addCleanup(lock.unlink, True)
        self.assertFalse(local_review.break_stale_lock(lock))
        self.assertTrue(lock.exists(), "the lock was broken while another breaker held it")

    def test_a_fresh_lock_is_respected(self) -> None:
        """Otherwise the staleness escape hatch would defeat the lock it exists to rescue."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        lock = Path(str(local_review.receipt_path(self.tmp)) + ".lock")
        lock.write_text("", encoding="utf-8")
        self.addCleanup(lock.unlink, True)
        # Driving held_lock directly, with a short budget, so the case does not spend the real wait.
        with self.assertRaises(local_review.CannotRun):
            local_review.held_lock(local_review.receipt_path(self.tmp), timeout=0.2)

    def test_an_ignored_file_stays_out_of_the_key(self) -> None:
        """Otherwise build output would demand a review, and the docstring claims it does not."""
        (self.tmp / ".gitignore").write_text("build/\n", encoding="utf-8")
        run(self.tmp, "add", ".gitignore")
        run(self.tmp, "commit", "-m", "ignore build")
        before = self.digest()
        (self.tmp / "build").mkdir()
        (self.tmp / "build" / "out.o").write_text("junk\n", encoding="utf-8")
        self.assertEqual(self.digest(), before, "an ignored file entered the key")

    def test_adding_then_deleting_a_file_keeps_the_key(self) -> None:
        """The documented scope boundary: the key follows net content, not the commit series.

        Asserted rather than left implicit, because it is the property a reader is most likely to
        assume the other way round.
        """
        before = self.digest()
        (self.tmp / "temp.py").write_text("print('x')\n", encoding="utf-8")
        run(self.tmp, "add", "temp.py")
        run(self.tmp, "commit", "-m", "add temp")
        self.assertNotEqual(self.digest(), before)
        run(self.tmp, "rm", "-q", "temp.py")
        run(self.tmp, "commit", "-m", "remove temp")
        self.assertEqual(self.digest(), before, "the net-content scope is not what is documented")

    def test_two_staging_runs_over_unchanged_content_agree(self) -> None:
        """Recording then checking stages the same content twice, so the read has to be stable."""
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.digest(), self.digest())

    def test_a_future_receipt_version_reads_as_unusable(self) -> None:
        problems = local_review.receipt_problems(
            {
                "receiptVersion": local_review.RECEIPT_VERSION + 1,
                "target": "develop",
                "mergeBase": "x",
                "contentDigest": "y",
                "passes": [],
            }
        )
        self.assertTrue(any("receiptVersion" in p for p in problems))

    def test_the_receipt_lives_outside_the_working_tree(self) -> None:
        """So it can never be committed by accident and needs no .gitignore entry."""
        path = local_review.receipt_path(self.tmp)
        self.assertEqual(path.parent, local_review.git_dir(self.tmp))
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(run(self.tmp, "status", "--porcelain", "--ignored"), "")

    def test_a_linked_worktree_keeps_its_own_receipt(self) -> None:
        """Two tasks in two worktrees of one repository must not share a coverage answer."""
        other = self.outside / "linked"
        run(self.tmp, "worktree", "add", "-b", "other", str(other))
        self.addCleanup(run, self.tmp, "worktree", "remove", "--force", str(other))
        self.assertNotEqual(local_review.receipt_path(self.tmp), local_review.receipt_path(other))


class BackendCase(RepoCase):
    """Drives run_coderabbit against a stand-in binary, so the parsing contract is tested."""

    def fake_cli(
        self, stdout: str, code: int = 0, argv_log: Path | None = None, also: str = ""
    ) -> None:
        bin_dir = self.outside / "fakebin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "coderabbit"
        body = "#!/usr/bin/env python3\nimport sys\n"
        if argv_log is not None:
            body += f"open({str(argv_log)!r}, 'w').write(chr(10).join(sys.argv[1:]))\n"
        if also:
            body += also + "\n"
        body += f"sys.stdout.write({stdout!r})\nsys.exit({code})\n"
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
        prev = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{prev}"
        self.addCleanup(self.restore_env, "PATH", prev)

    def test_a_completed_run_counts_findings(self) -> None:
        self.fake_cli('{"type":"finding"}\n{"type":"finding"}\n{"type":"complete"}\n')
        self.assertEqual(local_review.run_coderabbit(self.target, self.tmp), (2, ""))

    def test_a_run_with_no_completion_event_is_not_a_review(self) -> None:
        """A build that does not understand --agent prints prose and exits zero.

        Treating that as a clean review records a pass over content nothing read.
        """
        self.fake_cli("Reviewing your changes...\nLooks good!\n")
        findings, error = local_review.run_coderabbit(self.target, self.tmp)
        self.assertEqual(findings, 0)
        self.assertIn("completion event", error)

    def test_a_truncated_run_is_not_a_review(self) -> None:
        self.fake_cli('{"type":"finding"}\n')
        _, error = local_review.run_coderabbit(self.target, self.tmp)
        self.assertTrue(error)

    def test_an_error_event_is_reported(self) -> None:
        self.fake_cli('{"type":"error","message":"rate limited"}\n{"type":"complete"}\n')
        _, error = local_review.run_coderabbit(self.target, self.tmp)
        self.assertEqual(error, "rate limited")

    def test_a_non_zero_exit_with_no_error_event_still_reports(self) -> None:
        self.fake_cli('{"type":"complete"}\n', code=3)
        _, error = local_review.run_coderabbit(self.target, self.tmp)
        self.assertIn("exited 3", error)

    def test_a_rate_limited_run_records_no_pass(self) -> None:
        """A budget exhaustion shares its shape with a clean review, so it must not become one."""
        self.fake_cli('{"type":"error","message":"rate limited"}\n')
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 2
        )
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_a_completed_run_records_a_pass_through_main(self) -> None:
        """The whole cmd_run path, including the digest re-check and the receipt write."""
        self.fake_cli('{"type":"finding"}\n{"type":"complete"}\n')
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 0
        )
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 0)
        receipt, _ = local_review.read_receipt(self.tmp)
        assert receipt is not None
        self.assertEqual(receipt["passes"][0]["findings"], 1)

    def test_the_backend_is_given_the_merge_base_not_the_target_tip(self) -> None:
        """The two differ the moment the target moves, and only one is what the receipt claims.

        The fake CLI records its own argv, since asserting on the exit code alone would let the
        argument contract drift back to the tip with nothing objecting.
        """
        argv_log = self.outside / "argv.txt"
        self.fake_cli('{"type":"complete"}\n', argv_log=argv_log)
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.commit_all("a")
        base, _, _ = local_review.current_state(self.target, self.tmp)
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 0
        )
        argv = argv_log.read_text(encoding="utf-8").split("\n")
        self.assertIn("--agent", argv)
        self.assertIn(base, argv, f"the backend was not given the merge base: {argv}")
        self.assertNotIn("origin/develop", argv, "the backend was given the target tip")
        # The flag name matters as much as the value.
        # The CLI documents --base as taking a branch and --base-commit as taking a commit hash.
        # A sha handed to --base is the wrong call even though the sha itself is right.
        self.assertEqual(argv[argv.index(base) - 1], "--base-commit", argv)

    def test_content_changing_during_a_run_is_a_verdict(self) -> None:
        """The review covered content that no longer exists, which is an answer, not a boundary."""
        marker = self.tmp / "moved.py"
        self.fake_cli('{"type":"complete"}\n', also=f"open({str(marker)!r}, 'w').write('later\\n')")
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        self.commit_all("a")
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 1
        )
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 1)

    def test_the_backend_is_asked_to_include_untracked_files(self) -> None:
        """The receipt scope includes them and the CLI excludes them by default.

        Without the flag a pass would claim coverage of files the run never read, and a changed
        set of only untracked files would record a clean review of nothing.
        """
        argv_log = self.outside / "argv2.txt"
        self.fake_cli('{"type":"complete"}\n', argv_log=argv_log)
        (self.tmp / "untracked.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 0
        )
        self.assertIn("--include-untracked", argv_log.read_text(encoding="utf-8").split("\n"))

    def test_a_skipped_review_is_not_a_review(self) -> None:
        """The CLI completes with review_skipped when it found nothing to look at.

        Counting that as a review records a clean pass over content nothing read, which is the
        same false clean the missing-completion case guards against.
        """
        self.fake_cli('{"type":"complete","status":"review_skipped","findings":0}\n')
        (self.tmp / "a.py").write_text("x\n", encoding="utf-8")
        _, error = local_review.run_coderabbit("HEAD", self.tmp)
        self.assertIn("review_skipped", error)

    def test_a_missing_binary_is_a_boundary_through_main(self) -> None:
        prev = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.tmp / "definitely-empty")
        self.addCleanup(self.restore_env, "PATH", prev)
        self.assertEqual(
            self.main_quiet(["run", "--backend", "coderabbit-cli", "--target", self.target]), 2
        )


class ExitCodeCase(RepoCase):
    def test_an_unknown_reviewer_is_a_boundary_not_a_finding(self) -> None:
        """2 says the check did not run, which a gate must not read as either verdict."""
        self.assertEqual(
            self.main_quiet(
                ["record", "--reviewer", "nope", "--target", self.target, "--expect-digest", "x"]
            ),
            2,
        )

    def test_an_agent_backend_cannot_be_run_headlessly(self) -> None:
        self.assertEqual(
            self.main_quiet(["run", "--backend", "agent-skill", "--target", self.target]), 2
        )

    def test_an_unresolvable_target_is_a_boundary_not_a_finding(self) -> None:
        self.assertEqual(self.main_quiet(["check", "--target", "no-such-branch"]), 2)

    def test_an_empty_target_is_a_boundary_not_a_silent_default(self) -> None:
        """A hook written with an unset variable must not gate the wrong diff.

        The exit code alone does not prove the guard: an empty target also fails later, when the
        ref it builds does not resolve. The diagnostic is what the guard actually changes, so the
        assertion is on the message naming the empty value rather than an unresolvable ref.
        """
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = local_review.main(["check", "--target", ""])
        self.assertEqual(code, 2)
        self.assertIn("empty value", err.getvalue())

    def test_an_unexpected_crash_reports_the_boundary_not_a_verdict(self) -> None:
        """Falling through to the interpreter would exit 1, which reads as a real finding."""
        original = local_review.current_state

        def boom(*_args: object, **_kwargs: object) -> tuple[str, str, int]:
            raise RuntimeError("something nobody anticipated")

        # Suppressing assignment here: the stand-in raises rather than returning.
        local_review.current_state = boom  # type: ignore[assignment]
        self.addCleanup(setattr, local_review, "current_state", original)
        self.assertEqual(self.main_quiet(["check", "--target", self.target]), 2)

    def test_status_does_not_gate_on_coverage(self) -> None:
        """It reports, so a hook or skill step running it under `set -e` must not abort."""
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(self.main_quiet(["status", "--target", self.target]), 0)

    def test_status_emits_json_carrying_the_reviewers(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        self.record("agent-skill", 2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = local_review.main(["status", "--target", self.target])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["reviewers"], ["agent-skill"])
        self.assertTrue(data["covered"])

    def test_record_reports_what_it_recorded(self) -> None:
        (self.tmp / "new.py").write_text("print('x')\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = local_review.main(
                [
                    "record",
                    "--reviewer",
                    "agent-skill",
                    "--findings",
                    "2",
                    "--expect-digest",
                    self.digest(),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("agent-skill", buf.getvalue())
        receipt, problems = local_review.read_receipt(self.tmp)
        self.assertEqual(problems, [])
        assert receipt is not None
        self.assertEqual(receipt["passes"][0]["findings"], 2)


if __name__ == "__main__":
    unittest.main()
