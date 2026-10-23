"""The audit's Markdown reading and its README checks, read with markdown-it-py."""

import itertools
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import markdown_it
import validate
from carried_text import extract_section, normalize, strip_sections


def docker_hub_description(slug):
    """The Docker Hub short description for a repo, or None if the image is absent (HTTP 404).

    A transient failure raises, so the caller reports it as unverified rather than passing.
    """
    owner, repo = slug.split("/", 1)
    url = f"https://hub.docker.com/v2/repositories/{owner.lower()}/{repo.lower()}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8")).get("description")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def undeclared_h2_headings(text, declared):
    """Level-two headings in `text` that `declared` does not name, folded and sorted."""
    h2s = {h.strip().lower() for level, h, _, _ in _atx_headings(text) if level == 2}
    return sorted(h2s - {d.strip().lower() for d in declared})


# CommonMark plus tables, with escapes kept as their own tokens so a line can be rebuilt from its source.
_MD = markdown_it.MarkdownIt("commonmark").enable("table").disable("text_join")


def _atx_headings(text):
    """`(level, raw text, first line, last line)` for each ATX heading, the form the region reader splits on."""
    tokens = _MD.parse(text)
    return [
        (int(t.tag[1]), tokens[i + 1].content, t.map[0], t.map[1])
        for i, t in enumerate(tokens)
        if t.type == "heading_open" and t.markup.startswith("#") and t.map
    ]


def references(text):
    """The reference definitions markdown-it reads from a document, keyed the way it resolves a link."""
    env = {}
    _MD.parse(text, env)
    return env.get("references", {})


def heading_texts(markdown):
    """Lowercased heading texts in a Markdown document, for case-insensitive section-presence matching."""
    return {h.strip().lower() for _, h, _, _ in _atx_headings(markdown)}


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_md_links(text, references=None):
    """`text` with each Markdown link reduced to its label, everything else kept as written."""
    out = []
    for tok in _MD.parseInline(text, {"references": dict(references or {})})[0].children or []:
        if tok.type in ("text", "html_inline"):
            out.append(tok.content)
        elif tok.type == "code_inline":
            out.append(tok.markup + tok.content + tok.markup)
        elif tok.type == "softbreak":
            out.append("\n")
        elif tok.type not in ("link_open", "link_close"):
            out.append(tok.markup)
    return "".join(out)


def title_and_intro(text):
    """The H1 title text and the intro region between it and the first H2, HTML comments stripped."""
    lines = _HTML_COMMENT.sub("", normalize(text)).split("\n")
    heads = _atx_headings("\n".join(lines))
    first = next((h for h in heads if h[0] == 1), None)
    if first is None:
        return None, ""
    end = next((h[2] for h in heads if h[0] == 2 and h[2] > first[2]), len(lines))
    return first[1].strip(), "\n".join(ln.rstrip() for ln in lines[first[3] : end]).strip("\n")


def tagline(intro):
    """The first line of a README/HISTORY intro region, the one canonical short description."""
    return intro.split("\n")[0]


# Text scans, since a parsed link keeps its target but not the reference name and position these checks read.
_MD_IMAGE_REF = re.compile(r"!\[[^\]]*\]\[([^\]]+)\]")

_MD_IMAGE_INLINE = re.compile(r"!\[[^\]]*\]\((\S+?)\)")

_LINK_DEF = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)

# A URI scheme needs more than one character so a `C:` drive letter is not read as one.
_URI_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]+:", re.IGNORECASE)


def unfenced_text(text):
    """`text` normalized with every fenced code block's lines removed, since markup in a sample is shown rather than used."""
    lines = normalize(text).split("\n")
    fenced = {
        i for t in _MD.parse("\n".join(lines)) if t.type == "fence" and t.map for i in range(*t.map)
    }
    return "\n".join(ln for i, ln in enumerate(lines) if i not in fenced)


def readme_region(text, heading):
    """extract_section against a comment-stripped, unfenced copy of the document, or None where the heading is absent."""
    return extract_section(_HTML_COMMENT.sub("", unfenced_text(text)), heading)


def shield_endpoints(region, defs):
    """The image URLs a Markdown region renders, from its `![alt][ref]` and `![alt](url)` uses."""
    region = region or ""
    urls = [defs[m.group(1)] for m in _MD_IMAGE_REF.finditer(region) if m.group(1) in defs]
    return urls + [m.group(1) for m in _MD_IMAGE_INLINE.finditer(region)]


def shield_matches(url, shield):
    """True where a rendered URL is the shield the model describes, by endpoint plus its query discriminators."""
    return (
        shield["match"] in url
        and all(q in url for q in shield.get("requireQuery", []))
        and (shield.get("forbidQuery") is None or shield["forbidQuery"] not in url)
    )


def addressed_region(text, address):
    """The region a shield's `in` address names, as `Heading` or `Heading > Sub-heading`, or None if absent."""
    parts = [p.strip() for p in address.split(">")]
    body = readme_region(text, parts[0])
    if body is None or len(parts) == 1:
        return body
    lines = body.split("\n")
    h3s = [h for h in _atx_headings(body) if h[0] == 3]
    for i, (_, name, _, end) in enumerate(h3s):
        if name.strip().lower() == parts[1].lower():
            stop = h3s[i + 1][2] if i + 1 < len(h3s) else len(lines)
            return "\n".join(lines[end:stop])
    return None


def ordered_headings(markdown, level):
    """Heading texts at `level` in document order, HTML comments stripped, fenced blocks skipped."""
    return [
        _HTML_COMMENT.sub("", h).strip() for lv, h, _, _ in _atx_headings(markdown) if lv == level
    ]


def readme_section_findings(text, model, sel, public):
    """README section presence and order against spec/readme-sections.json."""
    findings = []
    by_name = {s["name"].lower(): s for s in model["sections"]}
    retired = {r.lower(): s for s in model["sections"] for r in s.get("retiredNames", [])}
    heads = ordered_headings(text, 2)
    lower = [h.lower() for h in heads]
    present = set(lower)

    for h, hl in zip(heads, lower):
        if hl in retired:
            findings.append(
                (
                    "LETTER",
                    f"readme: section '{h}' uses a retired name - rename it to '{retired[hl]['name']}', which has no accepted aliases (spec/readme-structure.md)",
                )
            )

    for s in model["sections"]:
        req = s["required"]
        if req == "optional" or (req == "public" and not public):
            continue
        if {t.lower() for t in s.get("notApplicableTo", [])} & {t.lower() for t in sel}:
            continue
        if s["name"].lower() in present or any(
            r.lower() in present for r in s.get("retiredNames", [])
        ):
            continue
        why = " (a public repo; optional while private)" if req == "public" else ""
        findings.append(
            (
                "LETTER",
                f"readme: no `## {s['name']}` section{why} - it is a required section (spec/readme-structure.md)",
            )
        )

    seq = [
        (by_name.get(hl) or retired.get(hl), h)
        for h, hl in zip(heads, lower)
        if hl in by_name or hl in retired
    ]
    for (prev, prev_h), (cur, cur_h) in itertools.pairwise(seq):
        if cur["ordinal"] < prev["ordinal"]:
            findings.append(
                (
                    "LETTER",
                    f"readme: section '{cur_h}' follows '{prev_h}' but is ordered before it - the declared sections keep their relative order (spec/readme-structure.md)",
                )
            )

    last = next((s for s in model["sections"] if s.get("last")), None)
    if last and last["name"].lower() in present and lower[-1] != last["name"].lower():
        findings.append(
            (
                "LETTER",
                f"readme: `## {last['name']}` is not the last section, '{heads[-1]}' follows it - it closes the file, immediately before the link definitions (spec/readme-structure.md)",
            )
        )

    for s in model["sections"]:
        if s["name"].lower() not in present or not s.get("subsections"):
            continue
        have = {h.lower() for h in ordered_headings(readme_region(text, s["name"]) or "", 3)}
        for sub in s["subsections"]:
            if sub.lower() not in have:
                findings.append(
                    (
                        "LETTER",
                        f"readme: `## {s['name']}` carries no `### {sub}` sub-section (spec/readme-structure.md)",
                    )
                )
    return findings


def distribution_prefixes(model, slug):
    """The URL prefixes that make a link this repo's own, from the model's single declaration of ownership."""
    owner = slug.split("/")[0]
    return tuple(
        p.replace("{slug}", slug).replace("{owner}", owner).lower()
        for p in model.get("distribution", {}).get("urlPrefixes", [])
    )


def link_kind(url, slug, is_rendered=False, prefixes=()):
    """Which linkGroups kind a reference definition points at, for the repo at `slug`."""
    if url.startswith("#"):
        return "anchor"
    if not url.startswith(("http://", "https://")) and not _URI_SCHEME.match(url):
        return "local"
    if prefixes and url.lower().startswith(tuple(prefixes)):
        return "distribution"
    return "shield" if is_rendered else "external"


def canonical_link_entry(url, model, slug):
    """The canonicalLinks entry `url` is a destination for, or None where the model fixes no name for it."""
    base = f"https://github.com/{slug}"
    bare = url.rstrip("/")
    for c in model.get("canonicalLinks", []):
        if "match" in c:
            if re.search(c["match"], url):
                return c
            continue
        if "repoPath" not in c:
            # Loud rather than skipped, since skipping would quietly stop enforcing this destination's name.
            raise KeyError(
                f"spec/readme-sections.json: canonicalLinks entry '{c.get('name')}' carries neither repoPath nor match"
            )
        want = (base + c["repoPath"]).rstrip("/")
        if (
            bare.lower().startswith(want.lower())
            if c.get("prefix")
            else bare.lower() == want.lower()
        ):
            return c
    return None


def canonical_name_findings(defs, model, slug, rendered=(), prefixes=()):
    """Reference names against canonicalLinks, resolved over the whole definition set rather than one at a time."""
    findings, hits = [], {}
    for ref, url in defs:
        if link_kind(url, slug, ref in rendered, prefixes) != "distribution":
            continue
        c = canonical_link_entry(url, model, slug)
        if c:
            hits.setdefault(c["name"], (c, []))[1].append((ref, url))
    for name, (c, group) in hits.items():
        if c.get("perTarget") and len(group) > 1:
            for ref, url in group:
                if not (ref.endswith(f"-{name}") and len(ref) > len(name) + 1):
                    findings.append(
                        (
                            "LETTER",
                            f"readme: the reference `[{ref}]` points at {url} - this repo publishes {len(group)} of these, so each is named `<target>-{name}` (spec/readme-structure.md)",
                        )
                    )
        else:
            for ref, url in group:
                if ref != name:
                    findings.append(
                        (
                            "LETTER",
                            f"readme: the reference `[{ref}]` points at {url} and is named `{name}` in every repo - a shared destination carries a shared name (spec/readme-structure.md)",
                        )
                    )
    return findings


def readme_link_findings(text, model, slug):
    """Reference-definition naming and grouping, per the linkGroups, linkNaming and canonicalLinks model."""
    findings = []
    suffixes = {n["kind"]: n["suffix"] for n in model["linkNaming"]}
    prefixes = distribution_prefixes(model, slug)
    groups = model["linkGroups"]
    declared = [g["name"] for g in groups]
    holds = {g["name"].lower(): g["holds"] for g in groups}

    unfenced = unfenced_text(text)
    seen_headers, current, in_group = [], None, {}
    for ln in unfenced.split("\n"):
        s = ln.strip()
        h = re.fullmatch(r"<!--\s*(.*?)\s*-->", s)
        if h and "omit from toc" not in h.group(1):
            if not any(
                h.group(1).lower().startswith(d.lower()) for d in model.get("directiveComments", [])
            ):
                current = h.group(1)
            continue
        d = _LINK_DEF.match(s)
        if d:
            if current is not None and current not in in_group:
                seen_headers.append(current)
            in_group.setdefault(current, []).append((d.group(1), d.group(2)))

    all_defs = [p for v in in_group.values() for p in v]
    # A reference the document renders as an image is a shield whatever host serves it, so resolve those first.
    by_ref = dict(all_defs)
    rendered = {m.group(1) for m in _MD_IMAGE_REF.finditer(unfenced) if m.group(1) in by_ref}
    described = {"shield": "a badge", "anchor": "an in-page anchor", "local": "a path in this repo"}
    for ref, url in all_defs:
        kind = link_kind(url, slug, ref in rendered, prefixes)
        want = suffixes.get(kind, "")
        got = "-shield" if ref.endswith("-shield") else "-link" if ref.endswith("-link") else ""
        if got != want:
            shown = f"`{want}`" if want else "a bare name with no suffix"
            findings.append(
                (
                    "LETTER",
                    f"readme: the reference `[{ref}]` points at {described.get(kind, 'a URI')} and should end in {shown} (spec/readme-structure.md)",
                )
            )
    findings += canonical_name_findings(all_defs, model, slug, rendered, prefixes)

    unknown = [h for h in seen_headers if h not in declared]
    if unknown:
        findings.append(
            (
                "DRIFT",
                f"readme: link-group header(s) outside the declared set: {', '.join(sorted(set(unknown)))} - the groups are {', '.join(declared)} (spec/readme-structure.md)",
            )
        )
    ordered = [h for h in seen_headers if h in declared]
    if ordered != sorted(ordered, key=declared.index):
        findings.append(
            (
                "DRIFT",
                f"readme: the link groups run {', '.join(ordered)} rather than the declared order (spec/readme-structure.md)",
            )
        )
    if in_group.get(None):
        if not seen_headers:
            findings.append(
                (
                    "DRIFT",
                    f"readme: the {len(in_group[None])} reference definitions carry no group headers - they are grouped under {', '.join(declared)} (spec/readme-structure.md)",
                )
            )
        else:
            findings.append(
                (
                    "DRIFT",
                    f"readme: {len(in_group[None])} reference definition(s) sit above the first group header (spec/readme-structure.md)",
                )
            )
    for header, defs in in_group.items():
        if header is None or not defs:
            continue
        names = [r for r, _ in defs]
        if names != sorted(names):
            findings.append(
                (
                    "DRIFT",
                    f"readme: the `{header}` group is not sorted by reference name (spec/readme-structure.md)",
                )
            )
        want_kind = holds.get(header.lower())
        strays = (
            {link_kind(u, slug, r in rendered, prefixes) for r, u in defs} - {want_kind}
            if want_kind
            else set()
        )
        if strays:
            findings.append(
                (
                    "DRIFT",
                    f"readme: the `{header}` group holds {', '.join(sorted(strays))} reference(s) where it holds {want_kind} (spec/readme-structure.md)",
                )
            )
    return findings


# A tool row is read as text for its reference name, with the outer pipes optional as GitHub's Markdown makes them.
_TOOL_ROW = re.compile(r"^\|?\s*\[([^\]]+)\]\[([^\]]+)\]\s*\|\s*([^|]*?)\s*(?:\||$)")

_TOOL_BULLET = re.compile(r"^[-*]\s*\[([^\]]+)\]\[([^\]]+)\]\s*(.*)$")


def third_party_tool_findings(text, catalog):
    """A repo's 3rd Party Tools entries against the shared catalog in spec/third-party-tools.json."""
    body = readme_region(text, "3rd Party Tools")
    if body is None:
        return []  # the absent section is already one finding from readme_section_findings
    declared = {t["name"].lower(): t for t in catalog["tools"]}
    # Read over the unfenced text, as readme_link_findings does: a sample footnote is not a definition.
    defs = {m.group(1): m.group(2) for m in _LINK_DEF.finditer(unfenced_text(text))}
    findings = []
    listed = []
    in_head = False
    for tok in _MD.parse(body):
        if tok.type in ("thead_open", "thead_close"):
            in_head = tok.type == "thead_open"
        elif in_head and tok.type == "inline" and tok.content.strip().lower() == "license":
            findings.append(
                (
                    "LETTER",
                    "readme: 3rd Party Tools carries a License column - a license belongs to the dependency and is authoritative at its source, so restating it adds a maintenance obligation and no information (spec/readme-structure.md)",
                )
            )
    for ln in body.split("\n"):
        m = _TOOL_ROW.match(ln.strip()) or _TOOL_BULLET.match(ln.strip())
        if not m:
            continue
        name, ref, desc = m.group(1), m.group(2), m.group(3).strip(" -:")
        listed.append(name)
        want = declared.get(name.lower())
        if want is None:
            continue
        url = defs.get(ref)
        if url is not None and url.rstrip("/") != want["link"].rstrip("/"):
            findings.append(
                (
                    "LETTER",
                    f"readme: 3rd Party Tools links {name} as {url} where the fleet links it as {want['link']} - a shared tool carries one link (spec/third-party-tools.json)",
                )
            )
        if desc != want["description"]:
            shown = f"'{desc}'" if desc else "no description"
            findings.append(
                (
                    "LETTER",
                    f"readme: 3rd Party Tools describes {name} as {shown} where the fleet describes it as '{want['description']}' - a shared tool carries one description (spec/third-party-tools.json)",
                )
            )
    # Ordering covers every tool listed, not only the cataloged ones, since a reader scans the whole list.
    if listed != sorted(listed, key=str.lower):
        first = next(
            f"{a} before {b}" for a, b in itertools.pairwise(listed) if a.lower() > b.lower()
        )
        findings.append(
            (
                "LETTER",
                f"readme: 3rd Party Tools is not alphabetized ({first}) - the list is scanned rather than read (spec/readme-structure.md)",
            )
        )
    return findings


def readme_shield_findings(text, model, entry):
    """Shield presence, by the additive classes in spec/readme-sections.json."""
    findings = []
    unfenced = unfenced_text(text)
    defs = {m.group(1): m.group(2) for m in _LINK_DEF.finditer(unfenced)}
    # A retired badge service is scanned across the whole document rather than per section, since a dead badge is wrong wherever it sits.
    rendered_urls = set(shield_endpoints(unfenced, defs))
    used_refs = {m.group(1) for m in _MD_IMAGE_REF.finditer(unfenced)}
    for dep in model.get("deprecatedShields", []):
        defined_urls = set()
        for ref, url in sorted(defs.items()):
            if dep["match"] in url:
                defined_urls.add(url)
                if ref in used_refs:
                    verb = f"renders {dep['label']}"
                elif url in rendered_urls:
                    verb = f"defines {dep['label']} and it is rendered elsewhere"
                else:
                    verb = f"defines {dep['label']} and nothing renders it"
                findings.append(
                    (
                        "LETTER",
                        f"readme: `[{ref}]` {verb}, which is retired - {dep['reason']} (spec/readme-structure.md)",
                    )
                )
        for url in sorted({u for u in rendered_urls if dep["match"] in u} - defined_urls):
            findings.append(
                (
                    "LETTER",
                    f"readme: an inline image renders {dep['label']}, which is retired - {dep['reason']} (spec/readme-structure.md)",
                )
            )
    targets = {(p.get("target") if isinstance(p, dict) else p) for p in entry.get("publish", [])}
    secrets = set(entry.get("requiredSecrets", []))
    want = []
    for cls in model["shieldClasses"]:
        t = cls["trigger"]
        if (
            t["kind"] == "always"
            or (t["kind"] == "publish" and t.get("target") in targets)
            or (t["kind"] == "secret" and t.get("name") in secrets)
        ):
            want += cls["shields"]
    for sh in want:
        region = addressed_region(text, sh["in"])
        if region is None:
            continue
        if not any(shield_matches(u, sh) for u in shield_endpoints(region, defs)):
            queries = "".join(f" with `{q}`" for q in sh.get("requireQuery", []))
            if sh.get("forbidQuery") is not None:
                queries += f" without `{sh['forbidQuery']}`"
            findings.append(
                (
                    "LETTER",
                    f"readme: `{sh['in']}` carries no {sh['label']} shield ({sh['match']}{queries}), which this repo's deliverables require (spec/readme-structure.md)",
                )
            )
        if not sh.get("exclusive"):
            continue
        # Rendered elsewhere, which for the license shield is the whole point: it belongs at the bottom and nowhere else.
        outside = strip_sections(
            _HTML_COMMENT.sub("", unfenced_text(text)), [sh["in"].split(">")[0].strip()]
        )
        if any(shield_matches(u, sh) for u in shield_endpoints(outside, defs)):
            findings.append(
                (
                    "LETTER",
                    f"readme: the {sh['label']} shield is rendered outside `{sh['in']}` - it belongs there and nowhere else (spec/readme-structure.md)",
                )
            )
    return findings


def description_findings(doc_texts, entry, live, slug):
    """The README title/tagline, the GitHub About description, and the Docker Hub short description, checked as one mirror set."""
    findings = []
    declared = None
    if "description" in entry:
        # Delegates to validate.py's own contract rather than a second copy of it.
        shape_errors = validate.description_errors(slug, entry["description"])
        if shape_errors:
            findings += [
                (
                    "DEFECT",
                    f"{msg}. Treated as undeclared here, since it fails spec/validate.py's contract.",
                )
                for msg in shape_errors
            ]
        else:
            declared = entry["description"]
    readme_want = None
    if "README.md" in doc_texts:
        title, intro = title_and_intro(doc_texts["README.md"])
        intro_line = tagline(intro)
        refs = references(doc_texts["README.md"])
        # The H1 is the repository name, and a hyphenated name may render its hyphens as spaces.
        repo_name = live.get("name") or slug.split("/")[-1]
        if not title:
            findings.append(
                (
                    "LETTER",
                    "readme: no `# ` H1 title - the README opens with `# <repo name>` then the tagline (spec/readme-structure.md)",
                )
            )
        elif title.replace("-", " ") != repo_name.replace("-", " "):
            findings.append(
                (
                    "LETTER",
                    f"readme: the H1 title '{title}' is not the repo name '{repo_name}' (a hyphenated name may render its hyphens as spaces) - the H1 is the repository name (spec/readme-structure.md)",
                )
            )
        if not intro_line:
            findings.append(
                (
                    "LETTER",
                    "readme: no tagline after the H1 - the README opens with the title then a one-line description, which doubles as the About description (spec/readme-structure.md)",
                )
            )
        else:
            if strip_md_links(intro_line, refs) != intro_line:
                findings.append(
                    (
                        "LETTER",
                        "readme: the tagline carries Markdown links - keep it link-free plain text, it doubles as the repo About description (spec/readme-structure.md)",
                    )
                )
            readme_want = strip_md_links(intro_line, refs).strip()
            if len(readme_want) > 100:
                findings.append(
                    (
                        "LETTER",
                        f"readme: the tagline is {len(readme_want)} characters, over the 100-char limit (Docker Hub's short-description cap, the tightest surface it feeds) - tighten it to one short sentence (spec/readme-structure.md)",
                    )
                )
    # The declared field wins once a repo has one, and every mirror is measured against it.
    want = declared or readme_want
    if want is None:
        return findings
    source = "registry/repos.json" if declared else "the README"
    if declared and readme_want is not None and readme_want != declared:
        findings.append(
            (
                "LETTER",
                f"readme: the tagline ('{readme_want}') does not match the declared description ('{declared}') - registry/repos.json's description is canonical once declared, so the README follows it (GOVERNANCE.md Repository Details)",
            )
        )
    desc = (live.get("description") or "").strip()
    if desc != want:
        findings.append(
            (
                "LETTER",
                f"description: the About description does not match the {'declared description' if declared else 'README tagline'} (description '{desc}' vs '{want}') - set it from {source}, or sharpen {source} first if the description carries real detail (GOVERNANCE.md Repository Details)",
            )
        )
    # Docker Hub short description mirrors the same canonical value, for a repo that publishes a docker image.
    if any(
        (pt.get("target") if isinstance(pt, dict) else pt) == "docker"
        for pt in entry.get("publish", [])
    ):
        try:
            dh = docker_hub_description(slug)
        except Exception as e:  # noqa: BLE001
            dh = None
            findings.append(
                (
                    "DRIFT",
                    f"description: could not read the Docker Hub short description to verify it mirrors {source} ({e}) - verify by hand",
                )
            )
        if dh is not None and dh.strip() != want:
            findings.append(
                (
                    "LETTER",
                    f"description: the Docker Hub short description ('{dh.strip()}') does not match '{want}' - set it from {source} (spec/readme-structure.md)",
                )
            )
    return findings
