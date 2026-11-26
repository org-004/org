"""JSON Schemas for the structured output that ends each engineer turn.

They are passed to the CLI with --json-schema, which makes the model's final answer a validated
object in the `result` event. The runner decides every state transition from these objects and
never from prose. The schemas stay plain (types, enums, required) so that the CLI's validator and
any later one agree on them.
"""
from __future__ import annotations

MILESTONE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "title", "est_minutes", "est_cost_usd"],
    "properties": {
        "id": {"type": "string", "description": "Short stable id, e.g. M1"},
        "title": {"type": "string"},
        "goal": {"type": "string", "description": "The state the milestone ends in"},
        "est_minutes": {"type": "number", "description": "Wall-clock minutes"},
        "est_cost_usd": {"type": "number", "description": "API-equivalent dollars, all models"},
        "parallelism": {"type": "integer", "description": "Widest fan-out planned inside the milestone"},
        "models": {"type": "array", "items": {"type": "string"}, "description": "Models that will do the work"},
        "verification": {"type": "string", "description": "How you will know it is done"},
    },
}

PLAN = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "estimate", "milestones"],
    "properties": {
        "verdict": {"type": "string", "enum": ["proceed", "infeasible"]},
        "summary": {"type": "string", "description": "The approach in two or three sentences"},
        "infeasible_reason": {"type": "string", "description": "Required when the verdict is infeasible"},
        "dropped_requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Requirement ids the plan leaves out to fit the caps",
        },
        "estimate": {
            "type": "object",
            "additionalProperties": False,
            "required": ["wall_minutes", "cost_usd"],
            "properties": {
                "wall_minutes": {"type": "number", "description": "Execution and report, after planning"},
                "cost_usd": {"type": "number"},
                "wall_minutes_p90": {"type": "number"},
                "cost_usd_p90": {"type": "number"},
                "basis": {"type": "string", "description": "What the estimate rests on"},
            },
        },
        "milestones": {"type": "array", "items": MILESTONE},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

MILESTONE_RESULT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["milestone_id", "outcome", "summary"],
    "properties": {
        "milestone_id": {"type": "string"},
        "outcome": {"type": "string", "enum": ["done", "partial", "blocked"]},
        "summary": {"type": "string", "description": "What was done and how it was verified"},
        "stop": {"type": "boolean", "description": "True to skip the remaining milestones and report"},
        "stop_reason": {"type": "string"},
        "revised_remaining": {
            "type": "array",
            "items": MILESTONE,
            "description": "Replaces every milestone not yet started. Omit when the plan stands.",
        },
    },
}

RESULT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "msg", "acceptance"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "fail"]},
        "msg": {"type": "string", "description": "The reason, in one or two sentences"},
        "acceptance": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "verdict", "evidence"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "fail", "unverified"]},
                    "evidence": {"type": "string"},
                },
            },
        },
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repo"],
                "properties": {
                    "repo": {"type": "string"},
                    "branch": {"type": "string"},
                    "pr_url": {"type": "string"},
                    "head": {"type": "string", "description": "Head commit of the delivered branch"},
                    "note": {"type": "string"},
                },
            },
        },
        "forecast_notes": {"type": "string", "description": "Why time and cost differed from the plan"},
        "remediations": {"type": "array", "items": {"type": "string"}},
    },
}

# What the review between two specs of a plan must answer (org/review.py, org/prompts/review.md).
# The lengths and the one-note-per-waiting-spec rule are checked in code, by review.check: this
# validator and the CLI's agree on types, enums and required keys only.
REVIEW = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["continue", "retry", "pause"]},
        "reason": {"type": "string", "description": "Why, in at most 300 characters; the person may read it on a phone"},
        "notes": {
            "type": "array",
            "description": "For `continue`: one entry for each waiting spec",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["spec", "note"],
                "properties": {
                    "spec": {"type": "string", "description": "The waiting spec's id"},
                    "note": {"type": "string", "description": "What that spec's engineer should know, at most 600 characters"},
                },
            },
        },
    },
}


TURN = {
    "type": "object",
    "additionalProperties": False,
    "required": ["did", "wake_in_min", "tell"],
    "properties": {
        "did": {"type": "array", "items": {"type": "string"},
                "description": "What this turn did, one plain sentence each; the first is what `org loop status` shows"},
        "wake_in_min": {"type": ["number", "null"], "minimum": 15, "maximum": 1440,
                        "description": "Wake me this many minutes after this turn ends, or null: a run's end or trouble wakes me anyway"},
        "tell": {"type": ["string", "null"],
                 "description": "One line for the owner's phone, or null: the day's plan, when the owner is needed, or a result worth a minute of theirs"},
    },
}


def problems(obj, schema, path="") -> list:
    """A small validator for the subset of JSON Schema used above.

    The CLI validates too; this second check is what lets the runner trust a plan it reads back
    from disk after a restart, and gives tests something to assert against.
    """
    out = []
    kind = schema.get("type")
    where = path or "(root)"
    if kind == "object":
        if not isinstance(obj, dict):
            return ["%s: expected an object" % where]
        for key in schema.get("required", []):
            if key not in obj:
                out.append("%s: missing %r" % (where, key))
        props = schema.get("properties", {})
        for key, value in obj.items():
            if key in props:
                out.extend(problems(value, props[key], (path + "." + key).lstrip(".")))
            elif schema.get("additionalProperties") is False:
                out.append("%s: unexpected key %r" % (where, key))
    elif kind == "array":
        if not isinstance(obj, list):
            return ["%s: expected an array" % where]
        for i, item in enumerate(obj):
            out.extend(problems(item, schema.get("items", {}), "%s[%d]" % (path, i)))
    elif kind == "string":
        if not isinstance(obj, str):
            out.append("%s: expected a string" % where)
        elif "enum" in schema and obj not in schema["enum"]:
            out.append("%s: %r is not one of %s" % (where, obj, schema["enum"]))
    elif kind == "number":
        if isinstance(obj, bool) or not isinstance(obj, (int, float)):
            out.append("%s: expected a number" % where)
    elif kind == "integer":
        if isinstance(obj, bool) or not isinstance(obj, int):
            out.append("%s: expected an integer" % where)
    elif kind == "boolean":
        if not isinstance(obj, bool):
            out.append("%s: expected a boolean" % where)
    return out
