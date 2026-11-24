"""Specs: the draft the manager refines, the checks it must pass, and the freeze at approval.

A spec is one Markdown file. A flat `key: value` header carries what the dispatcher needs (where to
run, the caps); the body carries what the engineer needs. One file, because the human approves
exactly what the engineer will read, and approval is bound to the file's sha256.
"""
from __future__ import annotations

import getpass
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .ledger import Ledger
from .util import now, read_json, read_text, sha256_bytes, slugify, write_json, write_text

TEMPLATE = """---
title: {title}
project: {project}
repos:
delivery: merge
machine:
max_cost_usd:
max_weekly_pct:
max_hours:
---

## Intent

What the human wants and why, in a paragraph or two: the spirit of the request. An engineer who
hits an ambiguity resolves it in favour of this section, so it has to be true.

## Context

The current state, as verified during refinement: what exists and where, what was run or measured,
which in-flight runs touch the same code.

## Requirements

- R1 (MUST): one testable statement per line.
- R2 (SHOULD): SHOULD items are what an engineer drops first when the full spec does not fit.

## Out of scope

- What an engineer might reasonably assume is wanted, and is not.

## Acceptance criteria

- AC1: a check someone else could run, with the command and the expected observation.

## Constraints

Repositories and branches, how the work is delivered, compatibility, performance, security,
machine needs, and the reasoning behind the caps in the header.

## Alternatives considered

- An option that was weighed, and why it lost.

## Open questions

None
"""

# Header keys and how each is read.
# `after` names the specs this one comes after: ids or numbers, checked against the ledger by
# org/graph.py. Its run waits in the queue until each of them has finished well.
LIST_KEYS = {"repos", "after"}
NUMBER_KEYS = {"max_cost_usd", "max_weekly_pct", "max_hours"}
# No `pr`: pull requests are not used. An engineer lands its own work (`merge`), or pushes a
# branch and stops (`branch`) when the person wants to look before anything reaches main.
ENUMS = {"delivery": {"branch", "merge", "none"}, "effort": {"low", "medium", "high", "xhigh", "max"}}
KNOWN_KEYS = {"title", "project", "repos", "delivery", "machine", "model", "effort", "after"} | NUMBER_KEYS

REQUIRED_SECTIONS = ["Intent", "Requirements", "Acceptance criteria"]
ADVISED_SECTIONS = ["Context", "Out of scope", "Constraints", "Alternatives considered"]

HEADER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
AC_RE = re.compile(r"^\s*[-*]\s*(?:\[[ xX]\]\s*)?(AC\d+)\b", re.M)
REQ_RE = re.compile(r"^\s*[-*]\s*(R\d+)\s*\((MUST|SHOULD)\)", re.M)
PLACEHOLDER_REPO_RE = re.compile(r"^(owner|org|user|your[-_]?\w*|example|todo|tbd|xxx)/|/(name|repo|todo|tbd)$|[<>{}?]|^(todo|tbd)$", re.I)
PLACEHOLDER_HINTS = [
    "one testable statement per line",
    "a check someone else could run",
    "What the human wants and why, in a paragraph or two",
]


def parse(text: str) -> Tuple[Dict[str, object], str]:
    """Split a spec into its header (typed) and body. Unknown keys are kept as strings."""
    text = text.replace("\r\n", "\n")
    m = HEADER_RE.match(text)
    if not m:
        return {}, text
    meta: Dict[str, object] = {}
    for line in m.group(1).split("\n"):
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip().lower(), raw.strip()
        if key in LIST_KEYS:
            meta[key] = [x.strip() for x in raw.split(",") if x.strip()]
        elif key in NUMBER_KEYS:
            if raw:
                try:
                    meta[key] = float(raw)
                except ValueError:
                    meta[key] = raw  # reported by check()
        elif raw:
            meta[key] = raw
    return meta, text[m.end():]


def sections(body: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    marks = list(SECTION_RE.finditer(body))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out[m.group(1).strip().lower()] = body[m.end():end].strip()
    return out


def acceptance_ids(body: str) -> List[str]:
    seen: List[str] = []
    for ac in AC_RE.findall(sections(body).get("acceptance criteria", "")):
        if ac not in seen:
            seen.append(ac)
    return seen


def check(text: str, default_machine: str = "incus", owner: str = "") -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). Errors block approval.

    `default_machine` is where the spec would run if its header does not say. Whether that
    machine exists is a question for a ledger: see check_draft.
    """
    errors: List[str] = []
    warnings: List[str] = []
    meta, body = parse(text)
    if not meta:
        errors.append("no header: the file must start with a --- key: value --- block")
    if not meta.get("title"):
        errors.append("header: title is required")
    for key in NUMBER_KEYS:
        if key in meta and not isinstance(meta[key], float):
            errors.append("header: %s must be a number, got %r" % (key, meta[key]))
        elif key in meta and meta[key] <= 0:
            errors.append("header: %s must be positive" % key)
    for key, allowed in ENUMS.items():
        if key in meta and meta[key] not in allowed:
            errors.append("header: %s must be one of %s" % (key, ", ".join(sorted(allowed))))
    for key in meta:
        if key not in KNOWN_KEYS:
            warnings.append("header: unknown key %r is ignored" % key)
    machine = str(meta.get("machine") or "")
    if machine and not re.match(r"^[a-z][a-z0-9]*(:[A-Za-z0-9_.-]+)?$", machine):
        errors.append("header: machine must be a kind or kind:name, such as incus or ssh:bigbox")
    for repo in meta.get("repos") or []:
        if PLACEHOLDER_REPO_RE.search(str(repo)):
            errors.append("header: repo %s is a placeholder; an engineer would have nothing to clone. "
                          "Put what is still unknown under Open questions until the person supplies it" % repo)
    # The engineer works on another machine. A path on this one is not a repository it can clone.
    where = machine or default_machine or "incus"
    if where != "local":
        for repo in meta.get("repos") or []:
            if str(repo).startswith(("/", "~", "./", "../", "file:")):
                errors.append("header: repo %s is a path on this machine, which an engineer on %s cannot reach. "
                              "Name its remote (owner/name or a URL); if it has none, it must be pushed first" % (repo, where))

    # Engineers push with a token that reaches one owner's repositories and no others. A
    # repository anywhere else can be read if it is public, and nothing can be delivered to it.
    if owner:
        for repo in meta.get("repos") or []:
            got = repo_owner(str(repo))
            if got and got.lower() != owner.lower():
                errors.append("header: repo %s is not in %s, the only place an engineer can push. Move or fork the "
                              "repository there, or leave it out of `repos` and name it under Context as something to read" % (repo, owner))

    secs = sections(body)
    for name in REQUIRED_SECTIONS:
        if not secs.get(name.lower()):
            errors.append("section '## %s' is missing or empty" % name)
    for name in ADVISED_SECTIONS:
        if not secs.get(name.lower()):
            warnings.append("section '## %s' is missing or empty" % name)

    if secs.get("acceptance criteria") and not acceptance_ids(body):
        errors.append("acceptance criteria need ids: list items that start with AC1, AC2, ...")
    reqs = REQ_RE.findall(secs.get("requirements", ""))
    if secs.get("requirements") and not reqs:
        errors.append("requirements need ids and priority: list items like '- R1 (MUST): ...'")
    elif reqs and not any(level == "MUST" for _, level in reqs):
        warnings.append("no requirement is marked MUST, so an engineer cannot tell what partial means")

    open_q = secs.get("open questions", "")
    if open_q and open_q.strip().lower().rstrip(".") not in ("none", "- none", "n/a"):
        errors.append("open questions remain: resolve them with the human or move them out of scope")

    for hint in PLACEHOLDER_HINTS:
        if hint in body:
            errors.append("template text is still present: %r" % hint)
            break
    if not any(k in meta for k in NUMBER_KEYS):
        warnings.append("no cap set (max_cost_usd, max_weekly_pct, max_hours): the run is unbounded")
    return errors, warnings


# ---------------------------------------------------------------- lifecycle


def repo_owner(repo: str) -> str:
    """The owner in owner/name or in a GitHub URL; "" for anything else (a path, another host)."""
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)?([A-Za-z0-9][A-Za-z0-9-]*)/[A-Za-z0-9._-]+?(?:\.git)?/?$", repo.strip())
    return m.group(1) if m else ""


def default_machine(ledger: Ledger) -> str:
    return str((ledger.config().get("machines") or {}).get("default") or "incus")


def check_text(ledger: Ledger, text: str, spec_id: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """check(), and whether this ledger could give the spec the machine it asks for.

    `spec_id` is whose text this is, when it is a spec of this ledger: a spec that names others in
    `after` is checked against them, and it must not name itself."""
    from . import backends  # here, not at the top: the engineer's side parses specs and starts no machines

    errors, warnings = check(text, default_machine(ledger), str((ledger.config().get("github") or {}).get("owner") or ""))
    if not any(e.startswith("header: machine") for e in errors):
        cfg = ledger.config()
        want = str(parse(text)[0].get("machine") or default_machine(ledger)).strip()
        try:
            backends.resolve(cfg, want).choose(cfg, want, "check")
        except backends.BackendError as e:
            errors.append("header: machine %s: %s" % (want, e))
    after = parse(text)[0].get("after")
    if after:
        from . import graph  # here for the same reason, and only for a spec that names another

        errors.extend(graph.check_after(ledger, spec_id, after))
    return errors, warnings


def check_draft(ledger: Ledger, spec_id: str) -> Tuple[List[str], List[str]]:
    return check_text(ledger, read_text(ledger.draft_path(spec_id)) or "", spec_id)


def new_draft(ledger: Ledger, title: str, project: str = "") -> str:
    ledger.init()
    spec_id = "%03d-%s" % (ledger.next_seq(), slugify(title))
    write_text(ledger.draft_path(spec_id), TEMPLATE.format(title=title.strip(), project=project.strip()))
    return spec_id


def draft_sha(ledger: Ledger, spec_id: str) -> Optional[str]:
    text = read_text(ledger.draft_path(spec_id))
    return None if text is None else sha256_bytes(text.encode("utf-8"))


class ApprovalError(Exception):
    pass


def approve(ledger: Ledger, spec_id: str, sha_prefix: str, by: Optional[str] = None) -> dict:
    """Freeze the draft as the approved spec. `sha_prefix` must match the draft as it is now.

    The hash is what ties the approval to the text the human read: if the draft changed after
    they looked at it, the prefix no longer matches and nothing is approved.
    """
    text = read_text(ledger.draft_path(spec_id))
    if text is None:
        raise ApprovalError("no draft for %s" % spec_id)
    sha = sha256_bytes(text.encode("utf-8"))
    sha_prefix = (sha_prefix or "").strip().lower()
    if len(sha_prefix) < 8:
        raise ApprovalError("give at least 8 characters of the draft's sha256 (it is %s)" % sha[:12])
    if not sha.startswith(sha_prefix):
        raise ApprovalError(
            "sha mismatch: the draft is now %s, not %s. It changed since it was reviewed." % (sha[:12], sha_prefix)
        )
    errors, _ = check_text(ledger, text, spec_id)
    if errors:
        raise ApprovalError("the draft does not pass its checks:\n  - " + "\n  - ".join(errors))

    meta, body = parse(text)
    sdir = ledger.spec_dir(spec_id)
    sdir.mkdir(parents=True, exist_ok=True)
    previous = read_json(sdir / "meta.json")
    version = 1
    if previous:
        if previous.get("sha256") == sha:
            return previous  # approving the same text twice is a no-op
        version = int(previous.get("version", 1)) + 1
        old = sdir / "spec.md"
        if old.exists():
            os.chmod(old, 0o644)
            os.replace(old, sdir / ("spec.v%d.md" % previous.get("version", 1)))
    write_text(sdir / "spec.md", text, mode=0o444)
    record = {
        "id": spec_id,
        "title": meta.get("title"),
        "version": version,
        "sha256": sha,
        "approved_at": now(),
        "approved_by": by or getpass.getuser(),
        "header": meta,
        "acceptance_ids": acceptance_ids(body),
    }
    write_json(sdir / "meta.json", record)
    return record


def state_of(ledger: Ledger, spec_id: str) -> str:
    """draft, approved, or revised (approved, and the draft has moved on since)."""
    meta = ledger.spec_meta(spec_id)
    if not meta:
        return "draft"
    sha = draft_sha(ledger, spec_id)
    if sha and sha != meta.get("sha256"):
        return "revised"
    return "approved"
