"""Document-text normalization helpers (shared by HTTP/document strategies).

Pure ``re``-based functions that turn scraper-extracted document text (item
numbers, actions, headings, paragraph glue) into clean, annotation-ready text.
Kept separate from :mod:`app.crawl.strategies.http` so the fetching code and the
document-text shaping code do not colocate, and so these helpers are unit-
testable in isolation.
"""

import re


def _format_extracted_document_text(text: str, *, source_url: str) -> str:
    """Normalize scraper-extracted document text for downstream annotation."""
    lines = [_clean_document_line(line) for line in text.splitlines()]
    records: list[str] = []
    action_by_item: dict[str, str] = {}
    index = 0
    while (line_index := _next_non_empty_document_line(lines, index)) is not None:
        # `index` stays *on* this line: the branches below advance it once they
        # are done, and two of them jump elsewhere entirely.
        index, line = line_index, lines[line_index]
        if _is_document_non_item_section(line):
            index += 1
            continue

        range_action_match = re.match(
            r"^(?P<start>\d+[a-z])\s*-\s*(?P<end>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE
        )
        if range_action_match:
            action = range_action_match.group("action").strip()
            for item_number in _document_item_range(
                range_action_match.group("start"), range_action_match.group("end")
            ):
                action_by_item[item_number] = action
            index += 1
            continue

        action_match = re.match(r"^(?P<number>\d+[a-z])\s+(?P<action>.+)$", line, flags=re.IGNORECASE)
        if action_match and not line.endswith("."):
            action_by_item[action_match.group("number").lower()] = action_match.group("action").strip()
            index += 1
            continue

        section_match = re.match(r"^(?P<number>\d+)\.$", line)
        if section_match:
            title_index = _next_non_empty_document_line(lines, index + 1)
            if title_index is not None and not _is_document_item_marker(lines[title_index]):
                records.append(f"{section_match.group('number')}. {lines[title_index]}")
                index = title_index + 1
                continue

        item_match = re.match(r"^(?P<number>\d+[a-z])\.$", line, flags=re.IGNORECASE)
        if item_match:
            number = item_match.group("number").lower()
            title_lines, next_index = _collect_document_item_lines(lines, index + 1)
            if title_lines:
                action = action_by_item.get(number)
                action_suffix = f" [Action: {action}]" if action else ""
                records.append(f"{number}. {' '.join(title_lines)}{action_suffix}")
                index = next_index
                continue

        records.append(line)
        index += 1

    return "\n\n".join(_merge_document_paragraph_records(records)).strip()


def _merge_document_paragraph_records(records: list[str]) -> list[str]:
    merged: list[str] = []
    for record in records:
        if merged and _should_join_document_paragraph(merged[-1], record):
            merged[-1] = _join_document_paragraph_lines(merged[-1], record)
            continue
        merged.append(record)
    return merged


def _should_join_document_paragraph(previous: str, current: str) -> bool:
    if _is_numbered_document_record(previous) or _is_numbered_document_record(current):
        return False
    if _looks_like_document_heading(previous) or _looks_like_document_heading(current):
        return False
    if re.match(r"^[,;:.!?)]", current):
        return True
    if (
        not re.search(r"[.!?:;)]$", previous)
        and _looks_like_document_prose(previous)
        and _looks_like_document_prose(current)
    ):
        return True
    return (
        _looks_like_document_prose(previous) and _looks_like_document_prose(current) and current[:1].islower()
    )


def _join_document_paragraph_lines(previous: str, current: str) -> str:
    if re.match(r"^[,;:.!?)]", current):
        return f"{previous}{current[0]} {current[1:].lstrip()}"
    return f"{previous} {current}"


def _looks_like_document_prose(record: str) -> bool:
    return bool(re.search(r"[a-z]", record)) and len(record.split()) >= 3


def _looks_like_document_heading(record: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", record)
    return bool(letters) and letters.upper() == letters and len(record.split()) <= 8


def _is_numbered_document_record(record: str) -> bool:
    return bool(re.match(r"^\d+[a-z]?\.\s", record, flags=re.IGNORECASE))


def _clean_document_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.replace("\f", " ")).strip()


def _next_non_empty_document_line(lines: list[str], start: int) -> int | None:
    """Index of the first non-blank line at or after ``start``, or ``None``.

    The funnel for this module's line scanners, returning the *index* on
    purpose: every caller must keep scanning from a position it chose, and
    both scanners also need to know which line they hold. Skipping blanks
    inline instead of asking here is what puts the obligation to advance past
    one -- the rule that keeps a scanner from spinning on it -- in the body,
    where nothing announces it.

    Blank means falsy after ``_clean_document_line``, which is what makes
    "blank" one rule across the module rather than a regex per scanner.
    """
    for index in range(start, len(lines)):
        if lines[index]:
            return index
    return None


def _is_document_item_marker(line: str) -> bool:
    return bool(re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE))


def _is_document_record_boundary(line: str) -> bool:
    return bool(
        re.match(r"^\d+\.$", line)
        or re.match(r"^\d+[a-z]\.?$", line, flags=re.IGNORECASE)
        or re.match(r"^\d+[a-z]\s+.+$", line, flags=re.IGNORECASE)
        or _is_document_non_item_section(line)
        or line.lower() == "attachments"
    )


def _is_document_non_item_section(line: str) -> bool:
    return line.upper().startswith("PUBLIC PARTICIPATION")


def _collect_document_item_lines(lines: list[str], start: int) -> tuple[list[str], int]:
    title_lines: list[str] = []
    index = start
    while (line_index := _next_non_empty_document_line(lines, index)) is not None:
        line = lines[line_index]
        index = line_index
        numbered_continuation = re.match(r"^\d+\.\s+(?P<text>.+)$", line)
        if numbered_continuation:
            title_lines.append(numbered_continuation.group("text"))
            index += 1
            continue
        if re.match(r"^\d+\.$", line):
            next_index = _next_non_empty_document_line(lines, index + 1)
            if next_index is not None and not _is_document_non_item_section(lines[next_index]):
                title_lines.append(lines[next_index])
                index = next_index + 1
                continue
            break
        if _is_document_record_boundary(line):
            break
        title_lines.append(line)
        index += 1
    return title_lines, index


def _document_item_range(start: str, end: str) -> list[str]:
    start_match = re.match(r"^(\d+)([a-z])$", start.lower())
    end_match = re.match(r"^(\d+)([a-z])$", end.lower())
    if not start_match or not end_match or start_match.group(1) != end_match.group(1):
        return [start.lower(), end.lower()]
    prefix = start_match.group(1)
    return [f"{prefix}{chr(code)}" for code in range(ord(start_match.group(2)), ord(end_match.group(2)) + 1)]
