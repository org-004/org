"""The ledger: everything the manager's side keeps on disk.

    $ORG_HOME/                 default ~/.org
      config.json               machines, budget calibration, model defaults
      projects.json             name -> repos
      desk/                     the only place the manager chat may write
        drafts/<spec-id>.md     specs under refinement
        projects/<name>.md      project briefs: conventions, test commands, lessons from past runs
        scratch/                throwaway clones and experiments
      specs/<spec-id>/          approved specs; written only by `org approve`
        spec.md                 read-only once approved
        meta.json               sha256, version, approval, parsed frontmatter
        spec.v<N>.md            earlier approved versions
      runs/<run-id>/            one engineer's attempt at one approved spec
        run.json                where it runs and what was last heard
        mirror/                 copy of the engineer's run directory, pulled by sync
      budget.json               weekly-limit samples and the calibrated dollars-per-percent
      chat/                     the dashboard chatbot's session

The desk is separate from specs/ and runs/ so that one rule protects the record: the manager chat
is confined to the desk by Claude Code's sandbox and permission settings, and approved specs and
run records are only ever written by this program.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .util import read_json, write_json, write_text

DEFAULT_CONFIG: Dict[str, Any] = {
    "claude_bin": "claude",
    "engineer": {
        "model": "fable",
        "effort": "high",
        # A turn that ends without its structured output is resumed with a nudge this many times.
        "max_turn_retries": 3,
        # No stream event at all for this long means a hung process: kill it and resume.
        "silence_timeout_min": 90,
        # Held back from a spec's cost cap so a run that hits the cap can still write its report.
        "report_reserve_usd": 3.0,
    },
    "manager": {
        "model": None,  # None: whatever the user's Claude Code settings say
        "effort": None,
        "ask_model": "sonnet",  # the dashboard's question box: status questions do not need the big model
        "ask_effort": "low",
        "sandbox": True,
        "sandbox_exclude": [],  # more command patterns to run outside the sandbox, under permission rules
        "permission_mode": None,
        "network_allow": ["github.com", "*.github.com", "*.githubusercontent.com"],
    },
    "machines": {
        # Where a spec runs when its frontmatter does not say: "orb", "ssh:<name>" or "local".
        "default": "orb",
        "orb": {"base": "base", "prefix": "eng-", "push_token": True},
        # name -> {"host": "user@host", "options": ["-p", "2222"], "push_token": false}
        "ssh": {},
        # Runs on this machine with no isolation. For trying org out and for its tests.
        "local": {"enabled": False},
    },
    "budget": {
        # API-equivalent dollars per 1% of the weekly limit. None: learned from finished runs.
        "usd_per_weekly_pct": None,
    },
    "pricing": {},
    # How long `org status` waits for a machine before showing it as last seen.
    "status": {"sync_timeout_s": 8},
    "dash": {"port": 8787, "sync_interval_s": 10},
}

# What `org init` writes: only the knobs a person is likely to turn. Everything else comes from
# DEFAULT_CONFIG at load time, so new defaults reach existing ledgers.
STARTER_CONFIG = {
    "machines": {"default": "orb", "orb": {"base": "base"}, "ssh": {}},
    "budget": {"usd_per_weekly_pct": None},
}

GITIGNORE = "desk/scratch/\nruns/*/mirror/events.jsonl\nchat/\n*.tmp\n"

SPEC_ID_RE = re.compile(r"^(\d{3,})-([a-z0-9][a-z0-9-]*)$")
RUN_ID_RE = re.compile(r"^(\d{3,}-[a-z0-9][a-z0-9-]*)-r(\d+)$")


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Ledger:
    def __init__(self, root=None):
        chosen = root or os.environ.get("ORG_HOME") or (Path.home() / ".org")
        self.root = Path(chosen).expanduser().resolve()

    # ------------------------------------------------------------ paths
    @property
    def desk(self) -> Path:
        return self.root / "desk"

    @property
    def drafts(self) -> Path:
        return self.desk / "drafts"

    @property
    def scratch(self) -> Path:
        return self.desk / "scratch"

    @property
    def briefs(self) -> Path:
        return self.desk / "projects"

    @property
    def specs(self) -> Path:
        return self.root / "specs"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def chat(self) -> Path:
        return self.root / "chat"

    @property
    def config_path(self) -> Path:
        return self.root / "config.json"

    @property
    def projects_path(self) -> Path:
        return self.root / "projects.json"

    @property
    def budget_path(self) -> Path:
        return self.root / "budget.json"

    def draft_path(self, spec_id: str) -> Path:
        return self.drafts / (spec_id + ".md")

    def spec_dir(self, spec_id: str) -> Path:
        return self.specs / spec_id

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def mirror_dir(self, run_id: str) -> Path:
        return self.runs / run_id / "mirror"

    # ------------------------------------------------------------ lifecycle
    def init(self) -> bool:
        """Create the layout. True when the ledger did not exist before."""
        fresh = not self.config_path.exists()
        for d in (self.drafts, self.scratch, self.briefs, self.specs, self.runs, self.chat):
            d.mkdir(parents=True, exist_ok=True)
        if fresh:
            write_json(self.config_path, STARTER_CONFIG)
        if not (self.root / ".gitignore").exists():
            write_text(self.root / ".gitignore", GITIGNORE)
        if not self.projects_path.exists():
            write_json(self.projects_path, {})
        return fresh

    def config(self) -> dict:
        return deep_merge(DEFAULT_CONFIG, read_json(self.config_path, {}) or {})

    def projects(self) -> dict:
        return read_json(self.projects_path, {}) or {}

    # ------------------------------------------------------------ specs
    def draft_ids(self) -> List[str]:
        if not self.drafts.is_dir():
            return []
        return sorted(p.stem for p in self.drafts.glob("*.md") if SPEC_ID_RE.match(p.stem))

    def approved_ids(self) -> List[str]:
        if not self.specs.is_dir():
            return []
        return sorted(p.name for p in self.specs.iterdir() if (p / "meta.json").exists())

    def spec_ids(self) -> List[str]:
        return sorted(set(self.draft_ids()) | set(self.approved_ids()))

    def next_seq(self) -> int:
        seqs = [int(SPEC_ID_RE.match(i).group(1)) for i in self.spec_ids()]
        return (max(seqs) + 1) if seqs else 1

    def resolve_spec(self, ref: str) -> str:
        """Accept a full id, its number ("7", "007"), a unique prefix, or the path of its draft."""
        ids = self.spec_ids()
        if "/" in ref or ref.endswith(".md"):
            ref = Path(ref).name[:-3] if ref.endswith(".md") else Path(ref).name
        if ref in ids:
            return ref
        if ref.isdigit():
            hits = [i for i in ids if int(SPEC_ID_RE.match(i).group(1)) == int(ref)]
        else:
            hits = [i for i in ids if i.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LookupError("no spec matches %r (known: %s)" % (ref, ", ".join(ids) or "none"))
        raise LookupError("%r is ambiguous: %s" % (ref, ", ".join(hits)))

    # ------------------------------------------------------------ runs
    def run_ids(self) -> List[str]:
        if not self.runs.is_dir():
            return []
        ids = [p.name for p in self.runs.iterdir() if (p / "run.json").exists()]

        def key(rid):
            m = RUN_ID_RE.match(rid)
            return (m.group(1), int(m.group(2))) if m else (rid, 0)

        return sorted(ids, key=key)

    def runs_for(self, spec_id: str) -> List[str]:
        return [r for r in self.run_ids() if RUN_ID_RE.match(r) and RUN_ID_RE.match(r).group(1) == spec_id]

    def next_run_id(self, spec_id: str) -> str:
        nums = [int(RUN_ID_RE.match(r).group(2)) for r in self.runs_for(spec_id)]
        return "%s-r%d" % (spec_id, (max(nums) + 1) if nums else 1)

    def resolve_run(self, ref: str) -> str:
        """Accept a run id, a unique prefix of one, or a spec reference (meaning its latest run)."""
        ids = self.run_ids()
        if ref in ids:
            return ref
        hits = [r for r in ids if r.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        try:
            spec_id = self.resolve_spec(ref)
        except LookupError:
            spec_id = None
        if spec_id:
            of_spec = self.runs_for(spec_id)
            if of_spec:
                return of_spec[-1]
            raise LookupError("spec %s has no runs yet" % spec_id)
        if not hits:
            raise LookupError("no run matches %r (known: %s)" % (ref, ", ".join(ids) or "none"))
        raise LookupError("%r is ambiguous: %s" % (ref, ", ".join(hits)))

    def run(self, run_id: str) -> dict:
        return read_json(self.run_dir(run_id) / "run.json", {}) or {}

    def save_run(self, run_id: str, record: dict) -> None:
        write_json(self.run_dir(run_id) / "run.json", record)

    def spec_meta(self, spec_id: str) -> Optional[dict]:
        return read_json(self.spec_dir(spec_id) / "meta.json")
