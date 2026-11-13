# History Rewrites: Re-identification Rules

**Every commit a history rewrite reaches needs a valid signature again. The pass that re-signs it re-identifies the committer of each commit that is not the rewriter's own.** The facts below about the tooling drive every rule after them.

- **A rewrite cascades to every descendant.** A commit's hash covers its parents' hashes. So changing one commit changes the hash of every commit below it, on every branch that holds it. Rewriting a commit near the root rewrites nearly the whole branch, however few commits the edit itself changes.
- **`git filter-repo` strips signatures and signs nothing.** Its documentation says a rewritten commit's signature "cannot continue to work" and is removed. A run strips the signature from every commit it processes, including one whose content it leaves alone. By default it processes every ref. So every commit it emits lands unsigned, and a require-signed-commits rule rejects it with GitHub reporting `reason: unsigned`.
- **GitHub verifies a signature against the commit's `committer`, not its `author`.** The REST API's `verification.reason` values `no_user`, `unverified_email`, and `bad_email` are each defined by the committer email. So a rewriter's valid signature over a commit still committed by a bot (`dependabot[bot]`, `github-actions[bot]`) or GitHub's own web-flow lands unverified.

These gates keep committer and signature aligned:

1. **State the reach before rewriting anything.** Rewrite only when a commit must change, since no scoping limits how far the new hashes reach. A `git filter-repo` run reaches every commit on every ref it processes. A tool given a range reaches the earliest changed commit and every descendant of it. `git rev-list --count --ancestry-path <earliest-target>..<branch>` counts those descendants per branch. Also list what is anchored to the old hashes, from "Collateral" below.
2. **Re-sign every rewritten commit in a signing pass that also sets the committer.** A tool that creates each commit through `git commit-tree -S` signs it. The command below re-signs every commit on every local branch and moves each tag with it. It keeps merge commits, authors, and both dates. Replace the placeholders with the rewriter's own signing identity:

   ```sh
   git filter-branch --tag-name-filter cat --commit-filter 'GIT_COMMITTER_NAME="<name>" GIT_COMMITTER_EMAIL="<email>" git commit-tree -S "$@"' -- --branches --tags
   ```

3. **Re-identify the committer, and keep the author where the tree is unchanged.** The command above sets the committer on every commit, which changes nothing on the rewriter's own. Leave `author` as the rewrite left it on a commit whose tree is unchanged, an identity correction included. Verification reads only the committer. Set `author` to the rewriter's identity only on a commit whose tree the rewrite alters, since the new tree is not the original author's work.
4. **Verify against GitHub, not the local checkout.** `git log --show-signature` checks a signature against the local keyring or allowed-signers file. It knows nothing of the account GitHub matches the committer to, so a commit can read good locally and still land unverified. Before the push, confirm the signing pass changed metadata only. Each branch's commit count and its `git log --format=%T` tree hashes match the values recorded between the rewrite and the signing pass. After the push, read every commit on each rewritten branch through the REST API and require `verified: true` with `reason: valid` on all of them. A single output line ending `true valid` is a pass, and any other line names a failing reason:

   ```sh
   gh api --paginate "repos/{owner}/{repo}/commits?sha=<branch>&per_page=100" --jq '.[].commit.verification | "\(.verified) \(.reason)"' | sort | uniq -c
   ```

Never leave a signature over a commit committed by another identity, and never leave a rewritten commit unsigned.

## Collateral

Everything anchored to an old hash goes stale when the rewrite lands:

- **Tags** point at the re-signed commits only where the signing pass moves them, as `--tag-name-filter cat` above does. A signed tag's signature no longer verifies after the rewrite.
- **Other clones** diverge from the rewritten branch, so each one is re-cloned rather than pulled.
- **Published artifacts** that record a commit hash keep naming a hash the branch no longer holds, and no rewrite reaches them.
