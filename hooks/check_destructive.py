#!/usr/bin/env python3
"""PreToolUse guard: block destructive shell/SQL commands; require human approval.

Plugin-resident (Tier A). Installed once, runs in every project. Reads no
{{template vars}} — any project-specific behavior is read from the project's
.claude/settings.json at RUNTIME.

Wiring: registered as a PreToolUse hook (matcher Bash|PowerShell) in
hooks/hooks.json, referencing ${CLAUDE_PLUGIN_ROOT}/hooks/check_destructive.py.

Contract (Claude Code hooks):
  - stdin  : JSON with tool_input.command (the shell/SQL string about to run).
  - stdout : on a match, a PreToolUse hookSpecificOutput with
             permissionDecision = "deny" and a human-readable reason.
  - exit 0 : always (a non-zero exit would surface as a hook error, not a
             clean deny). The decision is carried in the JSON payload.

Generic core (always on): SQL DROP/TRUNCATE/DELETE, git force-push,
git reset --hard, git clean -f, rm -rf, and common filesystem wipes.

Stack-specific patterns (databricks / supabase) are OFF by default and are
enabled per project via .claude/settings.json. Add either:

  {"destructiveGuard": {"stacks": ["databricks", "supabase"]}}

or, if you prefer to colocate it with the existing config:

  {"env": {"DESTRUCTIVE_GUARD_STACKS": "databricks,supabase"}}

The environment variable DESTRUCTIVE_GUARD_STACKS (comma-separated) takes
precedence over the settings.json value, so CI / one-off overrides work
without editing a file.

Requires: Python 3 on PATH.
"""
import json
import os
import re
import sys

# --- Generic destructive patterns (always enforced) -------------------------
# Each entry: (compiled-ready regex, human category). IGNORECASE applied below.
GENERIC_PATTERNS = [
    (r"\bDROP\s+(TABLE|SCHEMA|DATABASE|VIEW|INDEX)\b",
     "SQL DROP (TABLE/SCHEMA/DATABASE/VIEW/INDEX)"),
    (r"\bTRUNCATE\s+(TABLE\s+)?\w",
     "SQL TRUNCATE"),
    (r"\bDELETE\s+FROM\b",
     "SQL DELETE FROM"),
    (r"git\s+push\s.*(--force\b|--force-with-lease\b|-f\b)",
     "git force push"),
    (r"git\s+push\s+--force",
     "git force push"),
    (r"git\s+reset\s+--hard\b",
     "git reset --hard"),
    (r"git\s+clean\s+-[a-z]*f",
     "git clean -f"),
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r",
     "rm -rf"),
    (r"\brmdir\s+/s\b",
     "recursive rmdir /s"),
    (r"Remove-Item\b.*-Recurse\b.*-Force\b|Remove-Item\b.*-Force\b.*-Recurse\b",
     "PowerShell Remove-Item -Recurse -Force"),
]

# --- Stack-specific patterns (opt-in per project) ---------------------------
# Keyed by stack name; enabled only when that stack is listed in config.
STACK_PATTERNS = {
    "databricks": [
        (r"databricks\s+(repos|workspace|jobs|clusters|pipelines|catalogs|schemas|tables|warehouses)\s+delete",
         "Databricks destructive CLI (delete)"),
        (r"databricks\s+fs\s+rm\b",
         "Databricks DBFS rm"),
        (r"\bVACUUM\b.*\bRETAIN\b.*\bHOURS\b",
         "Delta VACUUM (permanent file removal)"),
    ],
    "supabase": [
        (r"supabase\s+db\s+reset\b",
         "Supabase db reset"),
        (r"supabase\s+projects?\s+delete\b",
         "Supabase project delete"),
        (r"supabase\s+branches?\s+delete\b",
         "Supabase branch delete"),
    ],
    # 'generic' stack contributes nothing beyond the always-on core.
    "generic": [],
    "web": [],
    "data": [],
}


def _project_dir() -> str:
    """Best-effort path to the project root the hook is running for."""
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def _enabled_stacks() -> list:
    """Resolve which stack-specific pattern sets are active.

    Precedence: DESTRUCTIVE_GUARD_STACKS env var > settings.json. Never raises;
    a missing/malformed config simply means "generic core only".
    """
    raw = os.environ.get("DESTRUCTIVE_GUARD_STACKS")
    if raw:
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    settings_path = os.path.join(_project_dir(), ".claude", "settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return []

    stacks = []
    guard = settings.get("destructiveGuard", {})
    if isinstance(guard, dict):
        cfg = guard.get("stacks", [])
        if isinstance(cfg, str):
            cfg = [cfg]
        if isinstance(cfg, list):
            stacks.extend(str(s).strip().lower() for s in cfg if str(s).strip())

    # Fall back to a single 'stack' field if present (web|data|generic + ext).
    single = settings.get("stack")
    if isinstance(single, str) and single.strip():
        stacks.append(single.strip().lower())

    return stacks


def _active_patterns():
    patterns = list(GENERIC_PATTERNS)
    for stack in _enabled_stacks():
        patterns.extend(STACK_PATTERNS.get(stack, []))
    return patterns


def _deny(category: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"BLOCKED: {category}. This is a destructive operation. "
                "Stop and ask the human for explicit approval before retrying."
            ),
        }
    }))


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except (ValueError, OSError):
        # Can't parse input -> fail open (don't block normal work on a parse error).
        return 0

    command = hook_input.get("tool_input", {}).get("command", "")
    if not command:
        return 0

    for pattern, category in _active_patterns():
        if re.search(pattern, command, re.IGNORECASE):
            _deny(category)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
