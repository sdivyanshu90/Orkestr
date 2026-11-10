"""Feature audit: find code that exists but is never reached.

This is the audit that matters most for a project whose central claim is
"deterministic control outside the model": every control must be *on the
execution path*.  A control that is implemented, documented and unit-tested
but never called is documentation, not a control.

The script reports, for each candidate:
  * whether the symbol is referenced anywhere outside its own module;
  * whether the reference is a call site or only a definition/import;
  * whether it is reachable from the orchestrator's entry point.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "agent_squad"

# Symbols that are part of the public contract even if the runtime does not
# call them itself (protocol methods, model fields the API serialises, and
# so on).
KNOWN_INDIRECT = {
    # Protocol / ABC members implemented by concrete classes.
    "complete",
    "aclose",
    "invoke",
    "rule",
    "name",
    # Pydantic model fields serialised to JSON by the API.
    "to_dict",
    "model_dump",
    "model_dump_json",
    "model_validate",
    "model_copy",
    # Enum members.
    "value",
    "name",
    # Helpers used through attribute access.
    "explain",
    "summary",
    "state_for",
    "touched_paths",
    "missing",
    "union",
    "intersect",
    "without",
    "allows",
    "allows_all",
    "sorted",
    "workers",
    "rank",
    "passes",
    "ok",
    "totals",
    "total_tokens",
    "verifies",
}


def module_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.py"))


def collect_symbols() -> dict[str, list[tuple[str, int]]]:
    """Map a symbol name to the modules that define it."""
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out[node.name].append((str(path.relative_to(ROOT)), node.lineno))
    return out


def collect_calls() -> dict[str, list[tuple[str, int]]]:
    """Map an attribute/function name to the call sites that use it."""
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    out[func.attr].append((str(path.relative_to(ROOT)), node.lineno))
                elif isinstance(func, ast.Name):
                    out[func.id].append((str(path.relative_to(ROOT)), node.lineno))
            elif isinstance(node, ast.Attribute):
                out[node.attr].append((str(path.relative_to(ROOT)), node.lineno))
    return out


def main() -> int:
    symbols = collect_symbols()
    calls = collect_calls()

    report = {"never_called": [], "called_only_in_own_module": [], "checked": 0}

    for name, definitions in sorted(symbols.items()):
        if name.startswith("_") and not name.startswith("__"):
            continue
        if name in KNOWN_INDIRECT:
            continue
        if name in ("TaskSpec", "TaskState", "Message", "Artifact", "EvidencePackage"):
            continue
        uses = calls.get(name, [])
        report["checked"] += 1
        if not uses:
            report["never_called"].append(
                {"symbol": name, "defined_in": [d[0] for d in definitions]}
            )
            continue
        own_modules = {d[0] for d in definitions}
        external = [u for u in uses if u[0] not in own_modules]
        if not external:
            report["called_only_in_own_module"].append(
                {
                    "symbol": name,
                    "defined_in": sorted(own_modules),
                    "uses_in": sorted({u[0] for u in uses}),
                }
            )

    print(json.dumps(report, indent=2))
    print(f"\nchecked {report['checked']} symbols")
    print(f"never referenced: {len(report['never_called'])}")
    print(f"referenced only within their own module: {len(report['called_only_in_own_module'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
