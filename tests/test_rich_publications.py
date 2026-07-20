from __future__ import annotations

import pytest

from app.rich_publications import rich_table, split_rich_table_messages, table_row


TEST_COLUMN_SPANS = (3, 1)


def test_split_rich_table_messages_splits_by_table_row_limit() -> None:
    participant_names = tuple(f"Участник {number}" for number in range(1, 6))
    rows = tuple(
        table_row(
            (participant_name, str(number)),
            alignments=("left", "right"),
            column_spans=TEST_COLUMN_SPANS,
        )
        for number, participant_name in enumerate(participant_names, start=1)
    )

    messages = split_rich_table_messages(
        first_header="<p>Первая часть</p>",
        continuation_header="<p>Продолжение</p>",
        first_caption="Участники",
        continuation_caption="Участники · продолжение",
        column_names=("Участник", "Очки"),
        alignments=("left", "right"),
        column_spans=TEST_COLUMN_SPANS,
        rows=rows,
        max_message_length=10_000,
        max_table_rows=2,
    )

    assert len(messages) == 3
    assert all(len(message) < 10_000 for message in messages)
    assert "<caption>Участники</caption>" in messages[0]
    assert "· продолжение" not in messages[0]
    for message in messages[1:]:
        assert "<caption>Участники · продолжение</caption>" in message

    for message in messages:
        assert "<table bordered striped>" in message
        assert '<th colspan="3" align="left">Участник</th>' in message
        assert '<th align="right">Очки</th>' in message
        assert message.count('<td colspan="3" align="left">') in (1, 2)

    combined_messages = "".join(messages)
    for participant_name in participant_names:
        assert combined_messages.count(f">{participant_name}</td>") == 1


def test_split_rich_table_messages_rejects_row_that_cannot_fit() -> None:
    oversized_row = table_row(
        ("Очень длинное имя " * 100, "1"),
        alignments=("left", "right"),
        column_spans=TEST_COLUMN_SPANS,
    )

    with pytest.raises(
        ValueError,
        match="A table row does not fit into one Rich Message",
    ):
        split_rich_table_messages(
            first_header="<p>Заголовок</p>",
            continuation_header="<p>Продолжение</p>",
            first_caption="Участники",
            continuation_caption="Участники · продолжение",
            column_names=("Участник", "Очки"),
            alignments=("left", "right"),
            column_spans=TEST_COLUMN_SPANS,
            rows=(oversized_row,),
            max_message_length=500,
            max_table_rows=10,
        )


@pytest.mark.parametrize(
    ("max_message_length", "max_table_rows", "expected_message"),
    (
        (0, 1, "Maximum Rich Message length must be positive"),
        (-1, 1, "Maximum Rich Message length must be positive"),
        (1_000, 0, "Maximum table row count must be positive"),
        (1_000, -1, "Maximum table row count must be positive"),
    ),
)
def test_split_rich_table_messages_rejects_nonpositive_limits(
    max_message_length: int,
    max_table_rows: int,
    expected_message: str,
) -> None:
    row = table_row(
        ("Анна", "1"),
        alignments=("left", "right"),
        column_spans=TEST_COLUMN_SPANS,
    )

    with pytest.raises(ValueError, match=expected_message):
        split_rich_table_messages(
            first_header="<p>Заголовок</p>",
            continuation_header="<p>Продолжение</p>",
            first_caption="Участники",
            continuation_caption="Участники · продолжение",
            column_names=("Участник", "Очки"),
            alignments=("left", "right"),
            column_spans=TEST_COLUMN_SPANS,
            rows=(row,),
            max_message_length=max_message_length,
            max_table_rows=max_table_rows,
        )


def test_rich_table_applies_matching_spans_to_headers_and_data_rows() -> None:
    column_spans = (4, 3, 1)
    row = table_row(
        ("Анна", "1:1 → Франция", "<b>+2</b>"),
        alignments=("left", "center", "right"),
        column_spans=column_spans,
    )

    rendered = rich_table(
        caption="Очки за матч",
        column_names=("Участник", "Прогноз", "Очки"),
        alignments=("left", "center", "right"),
        column_spans=column_spans,
        rows=(row,),
    )

    assert '<th colspan="4" align="left">Участник</th>' in rendered
    assert '<th colspan="3" align="center">Прогноз</th>' in rendered
    assert '<th align="right">Очки</th>' in rendered
    assert '<td colspan="4" align="left">Анна</td>' in rendered
    assert '<td colspan="3" align="center">1:1 → Франция</td>' in rendered
    assert '<td align="right"><b>+2</b></td>' in rendered
    assert 'colspan="1"' not in rendered


def test_table_row_applies_leaderboard_spans() -> None:
    rendered = table_row(
        ("🥇", "Анна", "9"),
        alignments=("center", "left", "right"),
        column_spans=(1, 5, 1),
    )

    assert '<td align="center">🥇</td>' in rendered
    assert '<td colspan="5" align="left">Анна</td>' in rendered
    assert '<td align="right">9</td>' in rendered
    assert 'colspan="1"' not in rendered


@pytest.mark.parametrize(
    ("cells", "alignments", "column_spans"),
    (
        (("Анна", "1"), ("left", "right"), (1,)),
        (("Анна",), ("left", "right"), (1,)),
        (("Анна",), ("left",), (1, 1)),
    ),
)
def test_table_row_rejects_mismatched_column_parameters(
    cells: tuple[str, ...],
    alignments: tuple[str, ...],
    column_spans: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="must have equal lengths"):
        table_row(
            cells,
            alignments=alignments,
            column_spans=column_spans,
        )


@pytest.mark.parametrize("invalid_span", (0, -1, True, 1.5, "2"))
def test_table_row_rejects_nonpositive_or_noninteger_span(
    invalid_span: object,
) -> None:
    with pytest.raises(ValueError, match="must be positive integers"):
        table_row(
            ("Анна",),
            alignments=("left",),
            column_spans=(invalid_span,),  # type: ignore[arg-type]
        )


def test_table_row_rejects_more_than_twenty_base_columns() -> None:
    with pytest.raises(ValueError, match="cannot exceed 20 columns"):
        table_row(
            ("Анна", "1"),
            alignments=("left", "right"),
            column_spans=(20, 1),
        )
