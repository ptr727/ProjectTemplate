# Content Import: Capturing a Source System (Hub-Only)

What an agent captures, and how it verifies the capture, when a repo's initial content comes from a live external system the repo replaces (a hosted blog, a wiki, a CMS). This doc is **hub-only**, so it is not carried downstream, and it describes work the hub does when standing a repo up rather than a fact about any one repo. [`STANDUP.md`][standup] owns the create-to-conformance procedure, and this covers the one input that procedure cannot re-derive: a source system is not under version control, so what is not captured before it stops serving is gone.

The three rules below are measured rather than predicted, from a WordPress-to-Hugo import of a 108-post site (intake #456). Read each count as the evidence for its rule, not as a constant to expect.

## Capture While the Source Is Live

**Capture first, and treat the capture as a deliverable rather than a step toward one.** The source is often paid for, rented, or already scheduled for shutdown, so the capture window closes on someone else's calendar. Hold the verified capture (the export, the localized external assets, a golden URL list, a manifest of content hashes) as the before-snapshot, and convert from that rather than from the live system, so every later check compares against a fixed reference instead of a moving one.

Every failure below produces a capture that **reconciles cleanly and is wrong**. Each one is a case where the artifact the source hands you agrees with itself, which is exactly why it cannot be the thing that gates.

## An Export Is Not a Media Capture

A content export carries what the source's own media library holds. A page can reference an asset the library never held, and that asset appears in no export at all.

- **Measured:** of 972 distinct media assets referenced by the content, 261 (27%) are hotlinked to a third-party host and absent from the export.
- **The inventory trap.** Half the referencing URLs are served through the CMS image proxy (`i0.wp.com/lh3.ggpht.com/...`), so a third-party asset carries a first-party hostname. An inventory keyed on the host counts those as library assets and reports full coverage of a set it never looked at.
- **Enumerate from the rendered pages, not from the export manifest.** Normalize responsive variants and generated size suffixes first, so one asset counts once rather than once per derivative.
- **Localizing externally hosted media is a required step**, not an optional pass. A third-party host is under no obligation to keep serving, and what the export omits is precisely what disappears with no notice and no error.

## A Sitemap Is Not the URL Contract

The sitemap is what the source advertises. The contract is what it serves, because every URL it answers is one an inbound link, a feed reader, or a search index may already hold.

- **Measured:** the sitemap lists 111 URLs against 1,051 served. The gap is taxonomy term pages, pagination, feeds, attachment pages, and date archives.
- **The silent breaker is a default rename.** The target generator serves taxonomy roots under different names than the source (plural `/tags/` and `/categories/` against singular `/tag/` and `/category/`), so 195 term URLs return 404 while the build reports success.
- **A URL that is unlisted and unlinked still exists.** 83 date archives appear in no sitemap and are linked from nowhere on the live site, and turn up only by deriving candidates and probing. Give every class a recorded disposition (render, redirect, or drop), so a dropped URL is a decision rather than an omission.
- **Where one URL shape is ambiguous, name the discriminator.** A bare one-segment path can be either a real page or a generated attachment page, and on this site 2 of 110 were real pages. The sitemap is the discriminator there, because it lists precisely the posts and pages.
- **Consumer-visible identifiers beyond URLs get the same treatment.** A feed reader keys on the item GUID byte for byte, so a GUID carrying an `http` scheme that the import "modernizes" to `https` marks every post unread for every subscriber. Preserve what a consumer keys on, or accept the breakage deliberately and record the blast radius.
- **The gate:** a committed golden URL list generated from the **live crawl**, never from the converter output, since a list derived from the output makes the check circular. Assert a floor on the list length before checking parity, because a truncated list makes every check below it pass vacuously. A missing URL is a hard failure with one annotation each, and an extra URL is a notice, which keeps the list append-only and the floor sound. Run it in CI and again against the exact tree about to deploy.

## A Fetch Over HTTP Is Not the Original

A source that serves optimized derivatives returns one at the original's URL, under the original's filename. This is the strongest of the three, because it produces silent quality loss rather than visible absence.

- **Measured:** 778 library files captured over HTTP, compared path for path against the official media export. All 778 paths present in both, 747 byte-identical, **31 different**, and 17.2 MB of image data that an HTTP capture alone would have lost. The worst case is a 1.7 MB photo returned as an 8 KB thumbnail at the same URL, a 205x reduction.
- **The false pass:** a file-count reconciliation reports 778 of 778 while 4% of the bytes are wrong. Nothing errors and nothing is missing, and the loss is visible only by opening the images.
- **Take library media from the official export, and verify by content hash against its manifest.** A count is not a verification. Import tooling whose media step is an HTTP download (a `--download-media` flag) inherits this defect, so the export is the source of record and the tool's fetch is at best a fallback for what the export omits.
- **One archive caveat:** an export archive may stream without its end-of-archive marker, so an integrity check that looks for the trailing zero blocks calls a complete archive corrupt. Verify by extracting and hashing the members, which is the check that matters anyway.

## The Shape All Three Share

The cheap check passes for the wrong reason. An inventory keyed on the host, a URL list read from the sitemap, and a reconciliation counted by file are each the artifact the source hands you, and each one agrees with itself. So the check that gates has to read the thing being claimed: the rendered pages for media, a live crawl for URLs, and content hashes for bytes.

That is the floor rule in [GOVERNANCE.md "Verification Discipline"][governance-verification-discipline] applied to an import, and it carries the same property the whole section is built around. Every failure here is green.

<!-- Repo -->

[governance-verification-discipline]: ../GOVERNANCE.md#verification-discipline
[standup]: ../STANDUP.md
