from __future__ import annotations

from collections.abc import Sequence
from html import escape

from aiogram.types import InputRichMessage


RICH_MESSAGE_MAX_LENGTH = 30_000
RICH_MESSAGE_MAX_TABLE_ROWS = 450
RICH_MESSAGE_MAX_TABLE_COLUMNS = 20


def rich_message(html: str) -> InputRichMessage:
    return InputRichMessage(html=html, skip_entity_detection=True)


def escape_rich_text(value: object) -> str:
    return escape(str(value))


def format_awarded_points(points: int) -> str:
    return f"+{points}" if points > 0 else "0"


def table_row(
    cells: Sequence[str],
    *,
    alignments: Sequence[str],
    column_spans: Sequence[int],
    header: bool = False,
) -> str:
    if not (len(cells) == len(alignments) == len(column_spans)):
        raise ValueError(
            "Table cells, alignments, and column spans must have equal lengths."
        )
    if any(
        not isinstance(span, int) or isinstance(span, bool) or span <= 0
        for span in column_spans
    ):
        raise ValueError("Table column spans must be positive integers.")
    if sum(column_spans) > RICH_MESSAGE_MAX_TABLE_COLUMNS:
        raise ValueError(
            f"A Rich Message table cannot exceed "
            f"{RICH_MESSAGE_MAX_TABLE_COLUMNS} columns."
        )
    tag = "th" if header else "td"
    rendered_cells = "".join(
        f'<{tag}{_column_span_attribute(span)} align="{alignment}">{cell}</{tag}>'
        for cell, alignment, span in zip(
            cells,
            alignments,
            column_spans,
            strict=True,
        )
    )
    return f"<tr>{rendered_cells}</tr>"


def rich_table(
    *,
    caption: str,
    column_names: Sequence[str],
    alignments: Sequence[str],
    column_spans: Sequence[int],
    rows: Sequence[str],
) -> str:
    header_row = table_row(
        tuple(escape_rich_text(name) for name in column_names),
        alignments=alignments,
        column_spans=column_spans,
        header=True,
    )
    return (
        "<table bordered striped>"
        f"<caption>{escape_rich_text(caption)}</caption>"
        f"{header_row}{''.join(rows)}"
        "</table>"
    )


def split_rich_table_messages(
    *,
    first_header: str,
    continuation_header: str,
    first_caption: str,
    continuation_caption: str,
    column_names: Sequence[str],
    alignments: Sequence[str],
    column_spans: Sequence[int],
    rows: Sequence[str],
    max_message_length: int = RICH_MESSAGE_MAX_LENGTH,
    max_table_rows: int = RICH_MESSAGE_MAX_TABLE_ROWS,
) -> tuple[str, ...]:
    if max_message_length <= 0:
        raise ValueError("Maximum Rich Message length must be positive.")
    if max_table_rows <= 0:
        raise ValueError("Maximum table row count must be positive.")
    if not rows:
        raise ValueError("At least one table row is required.")

    messages: list[str] = []
    current_rows: list[str] = []

    def render_part(candidate_rows: Sequence[str], *, continuation: bool) -> str:
        header = continuation_header if continuation else first_header
        caption = continuation_caption if continuation else first_caption
        return header + rich_table(
            caption=caption,
            column_names=column_names,
            alignments=alignments,
            column_spans=column_spans,
            rows=candidate_rows,
        )

    for row in rows:
        continuation = bool(messages)
        candidate_rows = (*current_rows, row)
        candidate = render_part(candidate_rows, continuation=continuation)
        if (
            len(candidate) <= max_message_length
            and len(candidate_rows) <= max_table_rows
        ):
            current_rows.append(row)
            continue

        if not current_rows:
            raise ValueError("A table row does not fit into one Rich Message.")
        messages.append(render_part(current_rows, continuation=continuation))
        current_rows = [row]
        continuation_candidate = render_part(current_rows, continuation=True)
        if len(continuation_candidate) > max_message_length:
            raise ValueError("A table row does not fit into one Rich Message.")

    messages.append(render_part(current_rows, continuation=bool(messages)))
    return tuple(messages)


def _column_span_attribute(span: int) -> str:
    return f' colspan="{span}"' if span > 1 else ""
