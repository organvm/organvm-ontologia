#!/usr/bin/env python3
"""Routing resolver — match artifact properties against routing-law.yaml.

Standalone CLI with zero dependencies beyond the stdlib. Reads routing-law.yaml
from the same directory as this script, matches the supplied property dimensions
against rules in declaration order (first match wins), and prints the target.

Usage:
    python3 resolve.py --function sort --material email
    python3 resolve.py --security sovereign
    python3 resolve.py --scope organ --pattern api
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal YAML parser — handles the subset used by routing-law.yaml
# ---------------------------------------------------------------------------


def _strip_quotes(val: str) -> str:
    """Remove surrounding single or double quotes."""
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _parse_yaml(text: str) -> dict:
    """Parse the YAML subset used in routing-law.yaml.

    Handles: scalar values, nested dicts, sequences of scalars,
    sequences of dicts (with nested dict values), comments, blank lines,
    quoted strings, and the {} empty-dict literal.
    """
    lines = text.split("\n")
    result, _ = _parse_mapping(lines, 0, 0)
    return result


def _indent(line: str) -> int:
    """Count leading spaces."""
    return len(line) - len(line.lstrip(" "))


def _next_content(lines: list[str], start: int) -> tuple[int, int] | None:
    """Find next non-blank, non-comment line. Returns (index, indent) or None."""
    i = start
    while i < len(lines):
        s = lines[i].strip()
        if s and not s.startswith("#"):
            return i, _indent(lines[i])
        i += 1
    return None


def _parse_mapping(lines: list[str], start: int, base: int) -> tuple[dict, int]:
    """Parse a YAML mapping at indent level `base`."""
    result: dict = {}
    i = start

    while i < len(lines):
        s = lines[i].strip()

        if not s or s.startswith("#"):
            i += 1
            continue

        ind = _indent(lines[i])
        if ind < base:
            break
        if ind > base:
            # Deeper line belongs to a child; skip (consumed by recursive calls)
            i += 1
            continue

        # Must be a key: value at our level
        if ":" not in s:
            i += 1
            continue

        colon = s.index(":")
        key = s[:colon].strip()
        val_part = s[colon + 1:].strip()

        # Strip inline comment
        if val_part.startswith("#"):
            val_part = ""

        if val_part:
            # Inline scalar or {}
            result[key] = {} if val_part == "{}" else _strip_quotes(val_part)
            i += 1
        else:
            # Block value — peek at next content line
            nxt = _next_content(lines, i + 1)
            if nxt is None or nxt[1] <= base:
                result[key] = ""
                i += 1
            elif lines[nxt[0]].strip().startswith("- "):
                result[key], i = _parse_sequence(lines, nxt[0], nxt[1])
            else:
                result[key], i = _parse_mapping(lines, nxt[0], nxt[1])

    return result, i


def _parse_sequence(lines: list[str], start: int, base: int) -> tuple[list, int]:
    """Parse a YAML sequence at indent level `base`."""
    result: list = []
    i = start

    while i < len(lines):
        s = lines[i].strip()

        if not s or s.startswith("#"):
            i += 1
            continue

        ind = _indent(lines[i])
        if ind < base:
            break
        if ind > base:
            # Belongs to current item's child block, skip (consumed by item parser)
            i += 1
            continue

        if not s.startswith("- "):
            break

        # Parse one sequence item
        item_text = s[2:].strip()

        if not item_text:
            # Bare "- " with block below
            nxt = _next_content(lines, i + 1)
            if nxt is not None and nxt[1] > ind:
                if lines[nxt[0]].strip().startswith("- "):
                    child, i = _parse_sequence(lines, nxt[0], nxt[1])
                else:
                    child, i = _parse_mapping(lines, nxt[0], nxt[1])
                result.append(child)
            else:
                result.append("")
                i += 1
        elif ":" in item_text:
            # Dict item — first key:value on the "- " line
            # The implicit block indent for continuation keys = ind + 2
            item_dict, i = _parse_item_mapping(lines, i, ind)
            result.append(item_dict)
        else:
            result.append(_strip_quotes(item_text))
            i += 1

    return result, i


def _parse_item_mapping(
    lines: list[str], start: int, dash_indent: int,
) -> tuple[dict, int]:
    """Parse a mapping that begins on a '- key: val' line.

    The first key:value is on the dash line itself. Continuation keys
    are at dash_indent + 2 (the column after '- ').
    """
    s = lines[start].strip()
    item_text = s[2:].strip()  # after "- "

    colon = item_text.index(":")
    first_key = item_text[:colon].strip()
    first_val = item_text[colon + 1:].strip()

    item: dict = {}

    if first_val.startswith("#"):
        first_val = ""

    if first_val:
        item[first_key] = {} if first_val == "{}" else _strip_quotes(first_val)
    else:
        # Block value under this key — peek
        child_base = dash_indent + 2
        nxt = _next_content(lines, start + 1)
        if nxt is not None and nxt[1] > child_base:
            # Nested block under first_key (e.g. match:\n  security: sovereign)
            if lines[nxt[0]].strip().startswith("- "):
                item[first_key], end = _parse_sequence(lines, nxt[0], nxt[1])
            else:
                item[first_key], end = _parse_mapping(lines, nxt[0], nxt[1])
            # After parsing the nested block, continue from `end` for sibling keys
            return _continue_item_keys(lines, end, dash_indent + 2, item)
        else:
            item[first_key] = ""

    # Parse continuation keys at dash_indent + 2
    return _continue_item_keys(lines, start + 1, dash_indent + 2, item)


def _continue_item_keys(
    lines: list[str], start: int, child_base: int, item: dict,
) -> tuple[dict, int]:
    """Parse remaining key:value pairs in a list-item mapping."""
    i = start

    while i < len(lines):
        s = lines[i].strip()

        if not s or s.startswith("#"):
            i += 1
            continue

        ind = _indent(lines[i])

        if ind < child_base:
            break

        if ind > child_base:
            # Deeper nesting — skip (consumed by recursive calls below)
            i += 1
            continue

        if s.startswith("- "):
            # New list item at parent level
            break

        if ":" not in s:
            i += 1
            continue

        colon = s.index(":")
        key = s[:colon].strip()
        val = s[colon + 1:].strip()

        if val.startswith("#"):
            val = ""

        if val:
            item[key] = {} if val == "{}" else _strip_quotes(val)
            i += 1
        else:
            nxt = _next_content(lines, i + 1)
            if nxt is not None and nxt[1] > child_base:
                if lines[nxt[0]].strip().startswith("- "):
                    item[key], i = _parse_sequence(lines, nxt[0], nxt[1])
                else:
                    item[key], i = _parse_mapping(lines, nxt[0], nxt[1])
            else:
                item[key] = ""
                i += 1

    return item, i


# ---------------------------------------------------------------------------
# Routing resolver
# ---------------------------------------------------------------------------

DIMENSIONS = ("function", "material", "pattern", "scope", "security")


def load_law(path: Path | None = None) -> dict:
    """Load and parse routing-law.yaml."""
    if path is None:
        path = Path(__file__).parent / "routing-law.yaml"
    return _parse_yaml(path.read_text())


def resolve(
    law: dict,
    *,
    function: str | None = None,
    material: str | None = None,
    pattern: str | None = None,
    scope: str | None = None,
    security: str | None = None,
) -> tuple[str, int | None]:
    """Match properties against rules, return (target, rule_id).

    First-match semantics: iterate rules in order, return the first
    where every key in the rule's match dict equals the corresponding
    supplied property. A rule with an empty match dict is the default.

    Returns ("~/Workspace/intake/", None) only if no rules at all are defined.
    """
    props = {
        "function": function,
        "material": material,
        "pattern": pattern,
        "scope": scope,
        "security": security,
    }
    supplied = {k: v for k, v in props.items() if v is not None}

    rules = law.get("rules", [])

    for rule in rules:
        match_dict = rule.get("match", {})

        if not isinstance(match_dict, dict):
            continue

        # Empty match dict = default catch-all
        if not match_dict:
            rule_id = rule.get("id")
            return rule["target"], int(rule_id) if rule_id else None

        # Every key in match_dict must be present AND equal in supplied
        matched = True
        for key, expected in match_dict.items():
            if key not in supplied or supplied[key] != expected:
                matched = False
                break

        if matched:
            rule_id = rule.get("id")
            return rule["target"], int(rule_id) if rule_id else None

    return "~/Workspace/intake/", None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Resolve artifact routing from routing-law.yaml",
    )
    parser.add_argument("--function", choices=None, help="Function dimension")
    parser.add_argument("--material", choices=None, help="Material dimension")
    parser.add_argument("--pattern", choices=None, help="Pattern dimension")
    parser.add_argument("--scope", choices=None, help="Scope dimension")
    parser.add_argument("--security", choices=None, help="Security dimension")
    parser.add_argument(
        "--law",
        type=Path,
        default=None,
        help="Path to routing-law.yaml (default: adjacent to this script)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    has_any = any(getattr(args, dim) is not None for dim in DIMENSIONS)
    if not has_any:
        parser.error("at least one dimension must be specified")

    law = load_law(args.law)
    target, rule_id = resolve(
        law,
        function=args.function,
        material=args.material,
        pattern=args.pattern,
        scope=args.scope,
        security=args.security,
    )

    if args.json:
        import json
        print(json.dumps({"target": target, "rule_id": rule_id}))
    else:
        note = ""
        if rule_id is not None:
            for rule in law.get("rules", []):
                rid = rule.get("id")
                if rid and int(rid) == rule_id and "note" in rule:
                    note = f" ({rule['note']})"
                    break
        print(f"{target}{note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
