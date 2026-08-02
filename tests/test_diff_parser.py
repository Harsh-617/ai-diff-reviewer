import pytest

from app.diff_parser import DiffParseError, parse_diff


def test_single_file_single_hunk():
    diff = (
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -1,5 +1,6 @@\n"
        " def foo():\n"
        "-    return 1\n"
        "+    return 2\n"
        "+    x = 1\n"
        " \n"
        " def bar():\n"
        "     return 3\n"
    )

    result = parse_diff(diff)

    assert result == [
        {"path": "src/foo.py", "line": 2, "content": "    return 2"},
        {"path": "src/foo.py", "line": 3, "content": "    x = 1"},
    ]


def test_single_file_two_hunks():
    diff = (
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1,5 +1,6 @@\n"
        " line1\n"
        "-line2\n"
        "+CHANGED_TWO\n"
        "+INSERTED_TOP\n"
        " line3\n"
        " line4\n"
        " line5\n"
        "@@ -26,5 +27,6 @@\n"
        " line26\n"
        " line27\n"
        " line28\n"
        "-line29\n"
        "+CHANGED_BOTTOM\n"
        " line30\n"
        "+INSERTED_BOTTOM\n"
    )

    result = parse_diff(diff)

    assert result == [
        {"path": "pkg/mod.py", "line": 2, "content": "CHANGED_TWO"},
        {"path": "pkg/mod.py", "line": 3, "content": "INSERTED_TOP"},
        {"path": "pkg/mod.py", "line": 30, "content": "CHANGED_BOTTOM"},
        {"path": "pkg/mod.py", "line": 32, "content": "INSERTED_BOTTOM"},
    ]


def test_two_files_no_cross_contamination():
    diff = (
        "--- a/fileA.py\n"
        "+++ b/fileA.py\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-beta\n"
        "+BETA_CHANGED\n"
        " gamma\n"
        "--- a/fileB.py\n"
        "+++ b/fileB.py\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO_CHANGED\n"
        " three\n"
    )

    result = parse_diff(diff)

    assert result == [
        {"path": "fileA.py", "line": 2, "content": "BETA_CHANGED"},
        {"path": "fileB.py", "line": 2, "content": "TWO_CHANGED"},
    ]


def test_pure_deletion_diff_returns_empty_list():
    diff = (
        "--- a/gone.py\n"
        "+++ b/gone.py\n"
        "@@ -1,3 +1,2 @@\n"
        " keep1\n"
        "-remove_me\n"
        " keep2\n"
    )

    result = parse_diff(diff)

    assert result == []


def test_new_file_diff():
    diff = (
        "--- /dev/null\n"
        "+++ b/new/thing.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+new line 1\n"
        "+new line 2\n"
        "+new line 3\n"
    )

    result = parse_diff(diff)

    assert result == [
        {"path": "new/thing.py", "line": 1, "content": "new line 1"},
        {"path": "new/thing.py", "line": 2, "content": "new line 2"},
        {"path": "new/thing.py", "line": 3, "content": "new line 3"},
    ]


def test_garbage_text_raises_diff_parse_error():
    with pytest.raises(DiffParseError):
        parse_diff("this is not a diff at all\njust some random text\nfoo bar baz")


def test_empty_string_raises_diff_parse_error():
    with pytest.raises(DiffParseError):
        parse_diff("")


def test_whitespace_only_raises_diff_parse_error():
    with pytest.raises(DiffParseError):
        parse_diff("   \n\n  ")
