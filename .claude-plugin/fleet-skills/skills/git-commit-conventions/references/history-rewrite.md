# History Rewrites: Re-identification Rules

**Every commit a history rewrite reaches needs a valid signature again. The pass that re-signs it re-identifies the committer of each commit that is not the rewriter's own.** The facts below about the tooling drive every rule after them.

- **A rewrite cascades to every descendant.** A commit's hash covers its parents' hashes. So changing one commit changes the hash of every commit below it, on every branch that holds it. Rewriting a commit near the root rewrites nearly the whole branch, however few commits the edit itself changes.
- **`git filter-repo` strips signatures and signs nothing.** Its documentation says a rewritten commit's signature "cannot continue to work" and is removed, and that a signed tag becomes an annotated tag. A run strips the signature from every commit it processes, including one whose content it leaves alone. By default it processes every ref. So every commit it emits lands unsigned, and a require-signed-commits rule rejects it with GitHub reporting `reason: unsigned`.
- **GitHub verifies a signature against the commit's `committer`, not its `author`.** The REST API's `verification.reason` values `no_user`, `unverified_email`, and `bad_email` are each defined by the committer email. So a rewriter's valid signature over a commit still committed by a bot (`dependabot[bot]`, `github-actions[bot]`) or GitHub's own web-flow lands unverified.

These gates keep committer and signature aligned:

1. **State the reach before rewriting anything.** Rewrite only when a commit must change, since no scoping limits how far the new hashes reach. A `git filter-repo` run reaches every commit on every ref it processes. A tool given a range reaches the earliest changed commit and every descendant of it. `git rev-list --count --ancestry-path <earliest-target>..<branch>` counts those descendants per branch. Also list what is anchored to the old hashes, from "Collateral" below.
2. **Re-sign every rewritten commit in a signing pass.** A tool that creates each commit through `git commit-tree -S` signs it. For example, `git filter-branch --commit-filter 'git commit-tree -S "$@"' -- --branches` re-signs every commit on every local branch. It keeps merge commits and both dates.
3. **Re-identify the committer, and keep the author where the content is unchanged.** In the signing pass, set `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` to the rewriter's own signing identity on every commit that is not the rewriter's own. Keep `author` on a commit whose content the rewrite leaves unchanged, since verification reads only the committer. Set `author` the same way only on a commit whose content the rewrite alters, since the new content is not the original author's.
4. **Verify against GitHub, not the local checkout.** `git log --show-signature` checks a signature against the local keyring or allowed-signers file. It knows nothing of the account GitHub matches the committer to, so a commit can read good locally and still land unverified. Before the push, confirm the rewrite changed metadata only: each branch's commit count and its `git log --format=%T` tree hashes match the values from before the rewrite. After the push, read every commit on each rewritten branch through the REST API and require `verified: true` with `reason: valid` on all of them. One output line reading `true valid` is a pass, and any other line names a failing reason:

   ```sh
   gh api --paginate "repos/{owner}/{repo}/commits?sha=<branch>&per_page=100" --jq '.[].commit.verification | "\(.verified) \(.reason)"' | sort | uniq -c
   ```

Never leave a signature over a commit committed by another identity, and never leave a rewritten commit unsigned.

## Collateral

Everything anchored to an old hash goes stale when the rewrite lands:

- **Tags** keep pointing at the old commits until each is re-pointed, and a signed tag the rewrite carries loses its signature.
- **Other clones** diverge from the rewritten branch, so each one is re-cloned rather than pulled.
- **Published artifacts** that record a commit hash keep naming a hash the branch no longer holds, and no rewrite reaches them.
