from unidiff import PatchSet
from unidiff.errors import UnidiffParseError


class DiffParseError(Exception):
    pass


def parse_diff(diff_text: str) -> list[dict]:
    try:
        patch_set = PatchSet(diff_text)
    except UnidiffParseError as exc:
        raise DiffParseError(str(exc)) from exc

    if len(patch_set) == 0:
        raise DiffParseError("no files found in diff text")

    added_lines = []
    for patched_file in patch_set:
        path = patched_file.path
        for hunk in patched_file:
            for line in hunk:
                if not line.is_added:
                    continue
                added_lines.append({
                    "path": path,
                    "line": line.target_line_no,
                    "content": line.value.rstrip("\n"),
                })

    return added_lines
