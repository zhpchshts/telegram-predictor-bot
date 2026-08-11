from __future__ import annotations

from itertools import product

import pytest

from app.publication_message_matrix import (
    champion_predictions_insights,
    champion_result_insight,
    contest_completion_insight,
    prediction_display_mode,
    series_result_insights,
    series_start_insight,
    swiss_result_insights,
)


@pytest.mark.parametrize(
    ("prediction_count", "always_statistics", "expected"),
    [
        (0, False, "empty"),
        (1, False, "personal"),
        (10, False, "personal"),
        (11, False, "statistics"),
        (0, True, "empty"),
        (1, True, "statistics"),
        (10, True, "statistics"),
        (11, True, "statistics"),
    ],
)
def test_prediction_display_mode_matrix(
    prediction_count: int,
    always_statistics: bool,
    expected: str,
) -> None:
    assert (
        prediction_display_mode(
            prediction_count,
            always_statistics=always_statistics,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("favorite_count", "total", "expected"),
    [
        (50, 100, "Мнения разделились почти поровну."),
        (55, 100, "Мнения разделились почти поровну."),
        (56, 100, "У Team Spirit небольшой перевес."),
        (64, 100, "У Team Spirit небольшой перевес."),
        (65, 100, "Team Spirit — явный фаворит прогнозов."),
        (84, 100, "Team Spirit — явный фаворит прогнозов."),
        (85, 100, "Почти все верят в Team Spirit."),
        (100, 100, "Почти все верят в Team Spirit."),
    ],
)
def test_series_start_matrix(
    favorite_count: int,
    total: int,
    expected: str,
) -> None:
    assert series_start_insight("Team Spirit", favorite_count, total) == expected


@pytest.mark.parametrize(
    ("winner_count", "total", "expected"),
    [
        (0, 100, "Победителя не выбрал никто."),
        (1, 100, "Получился неожиданный результат."),
        (39, 100, "Получился неожиданный результат."),
        (40, 100, "Перед серией мнения были почти поровну."),
        (59, 100, "Перед серией мнения были почти поровну."),
        (60, 100, "Большинство угадало победителя."),
        (84, 100, "Большинство угадало победителя."),
        (85, 100, "Почти все угадали победителя."),
        (100, 100, "Почти все угадали победителя."),
    ],
)
def test_series_result_winner_matrix(
    winner_count: int,
    total: int,
    expected: str,
) -> None:
    winner_text, _ = series_result_insights(
        winner_count=winner_count,
        exact_count=0,
        total=total,
    )
    assert winner_text == expected


@pytest.mark.parametrize(
    ("exact_count", "total", "expected"),
    [
        (0, 100, "Точный счёт не угадал никто."),
        (1, 100, "Точный счёт угадали немногие."),
        (24, 100, "Точный счёт угадали немногие."),
        (25, 100, "Точных прогнозов оказалось много."),
        (100, 100, "Точных прогнозов оказалось много."),
    ],
)
def test_series_result_exact_score_matrix(
    exact_count: int,
    total: int,
    expected: str,
) -> None:
    _, exact_text = series_result_insights(
        winner_count=max(exact_count, 1),
        exact_count=exact_count,
        total=total,
    )
    assert exact_text == expected


@pytest.mark.parametrize(
    ("votes", "expected_favorite"),
    [
        (
            (("Spirit", 40), ("Falcons", 40), ("Liquid", 20)),
            "У турнира несколько софаворитов: Falcons и Spirit.",
        ),
        (
            (("Spirit", 29), ("Falcons", 25), ("Liquid", 24), ("Aurora", 22)),
            "Явного фаворита нет, чаще остальных выбирали Spirit.",
        ),
        (
            (("Spirit", 30), ("Falcons", 25), ("Liquid", 20), ("Aurora", 25)),
            "Главный фаворит — Spirit.",
        ),
        (
            (("Spirit", 59), ("Falcons", 21), ("Liquid", 20)),
            "Главный фаворит — Spirit.",
        ),
        (
            (("Spirit", 60), ("Falcons", 20), ("Liquid", 20)),
            "Spirit — доминирующий фаворит прогнозов.",
        ),
    ],
)
def test_champion_predictions_favorite_matrix(
    votes: tuple[tuple[str, int], ...],
    expected_favorite: str,
) -> None:
    favorite, _ = champion_predictions_insights(votes, total=100)
    assert favorite == expected_favorite


@pytest.mark.parametrize(
    ("votes", "expected"),
    [
        (
            (("Spirit", 40), ("Falcons", 30), ("Liquid", 20), ("Aurora", 10)),
            "Прогнозы сосредоточены на нескольких командах.",
        ),
        (
            (
                ("Spirit", 30),
                ("Falcons", 25),
                ("Liquid", 20),
                ("Aurora", 15),
                ("Tundra", 10),
            ),
            "Поддержку получил широкий круг претендентов.",
        ),
    ],
)
def test_champion_predictions_diversity_matrix(
    votes: tuple[tuple[str, int], ...],
    expected: str,
) -> None:
    _, diversity = champion_predictions_insights(votes, total=100)
    assert diversity == expected


@pytest.mark.parametrize(
    ("champion_count", "votes", "expected"),
    [
        (0, (("Falcons", 60), ("Liquid", 40)), "Чемпиона не выбрал никто."),
        (
            40,
            (("Spirit", 40), ("Falcons", 35), ("Liquid", 25)),
            "Фаворит прогнозов оправдал ожидания.",
        ),
        (
            40,
            (("Spirit", 40), ("Falcons", 40), ("Liquid", 20)),
            "Победил один из софаворитов прогнозов.",
        ),
        (
            20,
            (("Falcons", 50), ("Spirit", 20), ("Liquid", 30)),
            "Победил заметный претендент, но не главный фаворит.",
        ),
        (
            19,
            (("Falcons", 51), ("Spirit", 19), ("Liquid", 30)),
            "Победу Spirit предсказывали немногие.",
        ),
    ],
)
def test_champion_result_matrix(
    champion_count: int,
    votes: tuple[tuple[str, int], ...],
    expected: str,
) -> None:
    assert (
        champion_result_insight(
            champion_name="Spirit",
            champion_count=champion_count,
            votes=votes,
            total=100,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        (39, "Швейцарский этап принёс много сюрпризов."),
        (40, "Большую часть проходов удалось предсказать."),
        (69, "Большую часть проходов удалось предсказать."),
        (70, "Прогнозы на швейцарский этап оказались очень точными."),
        (100, "Прогнозы на швейцарский этап оказались очень точными."),
    ],
)
def test_swiss_result_accuracy_matrix(points: int, expected: str) -> None:
    accuracy, _, _ = swiss_result_insights(
        total_points=points,
        prediction_count=100,
        maximum_points=1,
        perfect_count=0,
        least_supported_actual_count=25,
    )
    assert accuracy == expected


@pytest.mark.parametrize(
    ("perfect_count", "least_supported", "expected_perfect", "expected_surprise"),
    [
        (
            0,
            25,
            "Идеальный прогноз не собрал никто.",
            "Явной неожиданности среди прошедших команд не было.",
        ),
        (
            3,
            24,
            "Идеальный прогноз собрали: 3.",
            "Хотя бы одна прошедшая команда была настоящим сюрпризом.",
        ),
    ],
)
def test_swiss_result_secondary_matrix(
    perfect_count: int,
    least_supported: int,
    expected_perfect: str,
    expected_surprise: str,
) -> None:
    _, perfect, surprise = swiss_result_insights(
        total_points=50,
        prediction_count=100,
        maximum_points=1,
        perfect_count=perfect_count,
        least_supported_actual_count=least_supported,
    )
    assert perfect == expected_perfect
    assert surprise == expected_surprise


def test_all_twelve_swiss_statistics_combinations_are_reachable() -> None:
    rendered = {
        swiss_result_insights(
            total_points=accuracy_points,
            prediction_count=100,
            maximum_points=1,
            perfect_count=perfect_count,
            least_supported_actual_count=least_supported,
        )
        for accuracy_points, perfect_count, least_supported in product(
            (39, 50, 70),
            (0, 1),
            (24, 25),
        )
    }
    assert len(rendered) == 12


def test_all_thirteen_series_result_statistics_combinations_are_reachable() -> None:
    rendered = {
        series_result_insights(
            winner_count=winner_count,
            exact_count=exact_count,
            total=100,
        )
        for winner_count, exact_count in (
            [(0, 0)]
            + [
                (winner_count, exact_count)
                for winner_count in (39, 40, 60, 85)
                for exact_count in (0, 10, 25)
            ]
        )
    }
    assert len(rendered) == 13


def test_all_eight_champion_prediction_statistics_combinations_are_reachable() -> None:
    vote_sets = (
        (("A", 40), ("B", 40), ("C", 20)),
        (("A", 30), ("B", 30), ("C", 15), ("D", 15), ("E", 10)),
        (("A", 29), ("B", 25), ("C", 24), ("D", 22)),
        (("A", 29), ("B", 20), ("C", 19), ("D", 17), ("E", 15)),
        (("A", 40), ("B", 30), ("C", 30)),
        (("A", 40), ("B", 20), ("C", 15), ("D", 15), ("E", 10)),
        (("A", 60), ("B", 20), ("C", 20)),
        (("A", 60), ("B", 10), ("C", 10), ("D", 10), ("E", 10)),
    )
    rendered = {champion_predictions_insights(votes, total=100) for votes in vote_sets}
    assert len(rendered) == 8


@pytest.mark.parametrize(
    ("winner_points", "runner_up_points", "reason", "expected"),
    [
        (11, 10, None, "Всё решил один балл."),
        (12, 10, None, "Победа с отрывом в 2 балла."),
        (15, 10, None, "Победа с отрывом в 5 баллов."),
        (31, 10, None, "Победа с отрывом в 21 балл."),
        (10, 10, "exact_score", "Решающими стали точные счета."),
        (10, 10, "goal_difference", "Решающими стали правильные разницы."),
        (10, 10, "outcome", "Решающими стали правильные исходы."),
        (
            10,
            10,
            "drawn_advancing_team",
            "Решающими стали прогнозы на прошедшие команды.",
        ),
        (10, 10, "champion", "Решающим стал прогноз на чемпиона."),
        (10, 10, "draw", "Все показатели совпали. Победитель определён жребием."),
    ],
)
def test_contest_completion_matrix(
    winner_points: int,
    runner_up_points: int,
    reason: str | None,
    expected: str,
) -> None:
    assert (
        contest_completion_insight(
            winner_points=winner_points,
            runner_up_points=runner_up_points,
            tiebreak_reason=reason,
        )
        == expected
    )
