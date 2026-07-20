from __future__ import annotations

import pytest

from app.rich_publications import split_rich_table_messages, table_row


def test_split_rich_table_messages_splits_by_table_row_limit() -> None:
    participant_names = tuple(f"Участник {number}" for number in range(1, 6))
    rows = tuple(
        table_row(
            (participant_name, str(number)),
            alignments=("left", "right"),
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
        assert '<th align="left">Участник</th>' in message
        assert '<th align="right">Очки</th>' in message

    combined_messages = "".join(messages)
    for participant_name in participant_names:
        assert combined_messages.count(f">{participant_name}</td>") == 1


def test_split_rich_table_messages_rejects_row_that_cannot_fit() -> None:
    oversized_row = table_row(
        ("Очень длинное имя " * 100, "1"),
        alignments=("left", "right"),
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
    row = table_row(("Анна", "1"), alignments=("left", "right"))

    with pytest.raises(ValueError, match=expected_message):
        split_rich_table_messages(
            first_header="<p>Заголовок</p>",
            continuation_header="<p>Продолжение</p>",
            first_caption="Участники",
            continuation_caption="Участники · продолжение",
            column_names=("Участник", "Очки"),
            alignments=("left", "right"),
            rows=(row,),
            max_message_length=max_message_length,
            max_table_rows=max_table_rows,
        )
