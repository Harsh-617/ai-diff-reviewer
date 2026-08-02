from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

MAX_CHUNK_BYTES = 65536


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


def split_into_chunks(diff_text: str) -> list[str]:
    try:
        patch_set = PatchSet(diff_text)
    except UnidiffParseError as exc:
        raise DiffParseError(str(exc)) from exc

    if len(patch_set) == 0:
        raise DiffParseError("no files found in diff text")

    chunks: list[str] = []
    current_chunk = ""
    current_bytes = 0

    for patched_file in patch_set:
        file_text = str(patched_file)
        file_bytes = len(file_text.encode("utf-8"))

        if current_chunk and current_bytes + file_bytes > MAX_CHUNK_BYTES:
            chunks.append(current_chunk)
            current_chunk = ""
            current_bytes = 0

        current_chunk += file_text
        current_bytes += file_bytes

    if current_chunk:
        chunks.append(current_chunk)

    return chunks
