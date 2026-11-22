"""Price table and cost arithmetic.

The Claude Code CLI reports an authoritative cost per turn (`total_cost_usd`, list prices, also on
subscription login). That figure only arrives when a turn ends, and an engineer's turn can run for
an hour, so between turn ends the runner prices the streamed token counts with this table. Every
turn end replaces the estimate with the CLI's figure and re-derives a correction factor per model,
so a stale table shows up as a small transient error rather than a wrong total.

Engineers also get this table in their prompt: it is what makes "use a cheaper model for cheap
work" a calculation instead of a slogan.
"""
from __future__ import annotations

from typing import Dict, Optional

# USD per million tokens, Anthropic list prices as of 2026-11. Cache writes are a multiple of the
# input price: 1.25x for the 5-minute cache, 2x for the 1-hour cache (checked against the CLI's
# own cost figures for Sonnet 5.5 and Haiku 5.5 on 2026-11-22). Override in config.json "pricing".
PRICES: Dict[str, Dict[str, float]] = {
    "claude-fable-5-2": {"input": 8.00, "output": 40.00, "cache_read": 0.20},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01},
}

# Aliases the CLI accepts for --model and for a subagent's `model` parameter.
ALIASES = {
    "fable": "claude-fable-5-2",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


def table(overrides: Optional[dict] = None) -> Dict[str, Dict[str, float]]:
    t = {k: dict(v) for k, v in PRICES.items()}
    for model, row in (overrides or {}).items():
        t.setdefault(model, {}).update(row)
    return t


def price_for(model: Optional[str], prices: Dict[str, Dict[str, float]]) -> Optional[Dict[str, float]]:
    """Exact id, then alias, then id prefix, then model family. None when nothing matches."""
    if not model:
        return None
    if model in prices:
        return prices[model]
    if model in ALIASES and ALIASES[model] in prices:
        return prices[ALIASES[model]]
    for known in prices:
        if model.startswith(known):
            return prices[known]
    for family, canonical in ALIASES.items():
        if family in model and canonical in prices:
            return prices[canonical]
    return None


def usage_cost(model: Optional[str], usage: dict, prices: Dict[str, Dict[str, float]]) -> Optional[float]:
    """Cost in USD of one API message's `usage` block, or None for an unknown model."""
    p = price_for(model, prices)
    if p is None or not usage:
        return None
    inp, out = p["input"], p["output"]
    read = p.get("cache_read", inp * 0.1)
    cost = (usage.get("input_tokens") or 0) * inp
    cost += (usage.get("output_tokens") or 0) * out
    cost += (usage.get("cache_read_input_tokens") or 0) * read
    created = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    w5 = split.get("ephemeral_5m_input_tokens") or 0
    w1 = split.get("ephemeral_1h_input_tokens") or 0
    if w5 or w1:
        cost += w5 * inp * CACHE_WRITE_5M + w1 * inp * CACHE_WRITE_1H
    else:
        cost += created * inp * CACHE_WRITE_5M
    return cost / 1e6


def prompt_table(prices: Dict[str, Dict[str, float]]) -> str:
    """The table as engineers see it, cheapest first, with cost relative to the cheapest."""
    rows = []
    for alias, canonical in ALIASES.items():
        p = prices.get(canonical)
        if p:
            rows.append((p["output"], alias, canonical, p))
    rows.sort()
    if not rows:
        return "(no price table available)"
    floor = rows[0][0] or 1.0
    lines = [
        "| model (alias) | input $/Mtok | output $/Mtok | cache read $/Mtok | relative |",
        "|---|---|---|---|---|",
    ]
    for out, alias, canonical, p in rows:
        lines.append(
            "| %s (`%s`) | %.2f | %.2f | %.2f | %dx |"
            % (canonical, alias, p["input"], p["output"], p.get("cache_read", 0.0), round(out / floor))
        )
    return "\n".join(lines)
