from unidiff import PatchSet

from app.diff_parser import MAX_CHUNK_BYTES, parse_diff, split_into_chunks
from app.rules import run_mock_rules


def _make_file_diff(idx: int, num_lines: int, overrides: dict[int, str] | None = None) -> tuple[str, str]:
    """Build a synthetic git-style "new file" diff for one file.

    Returns (diff_text, path). Line i (0-based) is "line_{i:04d}" unless
    overridden. Added lines are 1-based, so line i lands at target_line_no i+1.
    """
    overrides = overrides or {}
    path = f"pkg/file_{idx:02d}.py"
    header = [
        f"diff --git a/{path} b/{path}\n",
        "new file mode 100644\n",
        "index 0000000..1111111\n",
        "--- /dev/null\n",
        f"+++ b/{path}\n",
        f"@@ -0,0 +1,{num_lines} @@\n",
    ]
    body = [f"+{overrides.get(i, f'line_{i:04d}')}\n" for i in range(num_lines)]
    return "".join(header + body), path


def test_diff_under_limit_is_single_chunk():
    diff_text, _ = _make_file_diff(0, 5)

    chunks = split_into_chunks(diff_text)

    assert len(diff_text.encode("utf-8")) < MAX_CHUNK_BYTES
    assert len(chunks) == 1
    assert chunks[0] == diff_text


def test_many_normal_files_split_without_breaking_a_file():
    # Each file is 11143 bytes; 5 fit in one 65536-byte chunk (55715 bytes),
    # a 6th would push it to 66858 > 65536, so it starts a new chunk.
    files = [_make_file_diff(i, 1000) for i in range(8)]
    diff_text = "".join(text for text, _ in files)
    file_size = len(files[0][0].encode("utf-8"))
    assert file_size < MAX_CHUNK_BYTES
    assert all(len(text.encode("utf-8")) == file_size for text, _ in files)

    chunks = split_into_chunks(diff_text)

    assert len(chunks) == 2
    assert "".join(chunks) == diff_text

    seen_paths = []
    for chunk in chunks:
        patch_set = PatchSet(chunk)
        seen_paths.extend(pf.path for pf in patch_set)

    assert seen_paths == [path for _, path in files]
    assert len(chunks[0].encode("utf-8")) == 5 * file_size
    assert len(chunks[1].encode("utf-8")) == 3 * file_size


def test_oversized_file_gets_its_own_chunk():
    big_text, big_path = _make_file_diff(1, 6000)
    small1_text, small1_path = _make_file_diff(2, 100)
    small2_text, small2_path = _make_file_diff(3, 100)
    assert len(big_text.encode("utf-8")) > MAX_CHUNK_BYTES
    assert len(small1_text.encode("utf-8")) < MAX_CHUNK_BYTES

    diff_text = small1_text + big_text + small2_text

    chunks = split_into_chunks(diff_text)

    assert len(chunks) == 3
    assert chunks[0] == small1_text
    assert chunks[1] == big_text
    assert chunks[2] == small2_text
    assert [pf.path for pf in PatchSet(chunks[1])] == [big_path]


def test_scan_findings_match_hand_computed_list_across_chunk_boundaries():
    files = [
        _make_file_diff(0, 1000, overrides={500: "eval(user_input)"}),
        _make_file_diff(1, 1000),
        _make_file_diff(2, 1000),
        _make_file_diff(3, 1000),
        _make_file_diff(4, 1000),
        _make_file_diff(5, 1000, overrides={300: "console.log(debug_value)"}),
        _make_file_diff(6, 1000),
        _make_file_diff(7, 1000, overrides={999: "# TODO fix this before merge"}),
    ]
    diff_text = "".join(text for text, _ in files)

    chunks = split_into_chunks(diff_text)
    assert len(chunks) > 1  # the diff actually spans a chunk boundary

    findings = run_mock_rules(parse_diff(diff_text))

    expected = [
        {
            "id": "MOCK-001:pkg/file_00.py:501",
            "ruleId": "MOCK-001",
            "path": "pkg/file_00.py",
            "line": 501,
            "severity": "critical",
            "category": "security",
            "title": "eval usage",
            "evidence": "eval(user_input)",
        },
        {
            "id": "MOCK-007:pkg/file_05.py:301",
            "ruleId": "MOCK-007",
            "path": "pkg/file_05.py",
            "line": 301,
            "severity": "low",
            "category": "style",
            "title": "console.log left in",
            "evidence": "console.log(debug_value)",
        },
        {
            "id": "MOCK-008:pkg/file_07.py:1000",
            "ruleId": "MOCK-008",
            "path": "pkg/file_07.py",
            "line": 1000,
            "severity": "low",
            "category": "style",
            "title": "unresolved marker",
            "evidence": "# TODO fix this before merge",
        },
    ]

    assert findings == expected
