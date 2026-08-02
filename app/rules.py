import re

RULES = {
    "MOCK-001": {"severity": "critical", "category": "security", "title": "eval usage"},
    "MOCK-002": {"severity": "critical", "category": "security", "title": "hardcoded credential"},
    "MOCK-003": {"severity": "high", "category": "security", "title": "SQL string concatenation"},
    "MOCK-004": {"severity": "high", "category": "correctness", "title": "swallowed exception"},
    "MOCK-005": {"severity": "medium", "category": "correctness", "title": "loose null comparison"},
    "MOCK-006": {"severity": "medium", "category": "performance", "title": "deep-clone via JSON"},
    "MOCK-007": {"severity": "low", "category": "style", "title": "console.log left in"},
    "MOCK-008": {"severity": "low", "category": "style", "title": "unresolved marker"},
    "MOCK-INJ": {"severity": "critical", "category": "security", "title": "prompt-injection content"},
}

_CREDENTIAL_RE = re.compile(
    r"(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]",
    re.IGNORECASE,
)
_SQL_STRING_RE = re.compile(
    r"""['"][^'"]*\b(SELECT|INSERT|UPDATE|DELETE)\b[^'"]*['"]""",
    re.IGNORECASE,
)
_NULL_COMPARISON_RE = re.compile(r"==\s*null|!=\s*null")
_CATCH_RE = re.compile(r"catch\s*\(.*?\)\s*\{")
_INJECTION_RE = re.compile(
    r"ignore previous instructions|disregard all prior|you are now",
    re.IGNORECASE,
)


def _make_finding(rule_id: str, path: str, line: int, evidence: str) -> dict:
    rule = RULES[rule_id]
    return {
        "id": f"{rule_id}:{path}:{line}",
        "ruleId": rule_id,
        "path": path,
        "line": line,
        "severity": rule["severity"],
        "category": rule["category"],
        "title": rule["title"],
        "evidence": evidence,
    }


def _consecutive_runs(parsed_lines: list[dict]) -> list[list[dict]]:
    runs = []
    current_run = []
    prev_path = None
    prev_line = None

    for entry in parsed_lines:
        if prev_path == entry["path"] and prev_line is not None and entry["line"] == prev_line + 1:
            current_run.append(entry)
        else:
            if current_run:
                runs.append(current_run)
            current_run = [entry]
        prev_path = entry["path"]
        prev_line = entry["line"]

    if current_run:
        runs.append(current_run)

    return runs


def _find_empty_catches(parsed_lines: list[dict]):
    for run in _consecutive_runs(parsed_lines):
        for i, entry in enumerate(run):
            match = _CATCH_RE.search(entry["content"])
            if not match:
                continue

            remainder = entry["content"][match.end():].strip()
            if remainder.startswith("}"):
                yield entry
                continue
            if remainder:
                continue

            for later in run[i + 1:]:
                stripped = later["content"].strip()
                if stripped == "}":
                    yield entry
                    break
                if stripped:
                    break


def run_mock_rules(parsed_lines: list[dict]) -> list[dict]:
    findings = []
    seen_ids = set()

    def add(rule_id: str, path: str, line: int, evidence: str) -> None:
        finding = _make_finding(rule_id, path, line, evidence)
        if finding["id"] in seen_ids:
            return
        seen_ids.add(finding["id"])
        findings.append(finding)

    for entry in parsed_lines:
        path, line, content = entry["path"], entry["line"], entry["content"]

        if "eval(" in content:
            add("MOCK-001", path, line, content)

        if _CREDENTIAL_RE.search(content):
            add("MOCK-002", path, line, content)

        if "+" in content and _SQL_STRING_RE.search(content):
            add("MOCK-003", path, line, content)

        if _NULL_COMPARISON_RE.search(content):
            add("MOCK-005", path, line, content)

        if "JSON.parse(JSON.stringify(" in content:
            add("MOCK-006", path, line, content)

        if "console.log(" in content:
            add("MOCK-007", path, line, content)

        if "TODO" in content or "FIXME" in content:
            add("MOCK-008", path, line, content)

        if _INJECTION_RE.search(content):
            add("MOCK-INJ", path, line, content)

    for entry in _find_empty_catches(parsed_lines):
        add("MOCK-004", entry["path"], entry["line"], entry["content"])

    return findings
