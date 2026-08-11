from __future__ import annotations

from typing import Literal, Sequence


PERSONAL_PREDICTION_LIMIT = 10

PredictionDisplayMode = Literal["empty", "personal", "statistics"]


def prediction_display_mode(
    prediction_count: int,
    *,
    always_statistics: bool = False,
) -> PredictionDisplayMode:
    if prediction_count < 0:
        raise ValueError("Prediction count cannot be negative.")
    if prediction_count == 0:
        return "empty"
    if always_statistics or prediction_count > PERSONAL_PREDICTION_LIMIT:
        return "statistics"
    return "personal"


def series_start_insight(
    favorite_name: str,
    favorite_count: int,
    total: int,
) -> str:
    _validate_count(favorite_count, total=total)
    other_count = total - favorite_count
    gap_count = abs(favorite_count - other_count)
    if gap_count * 100 <= total * 10:
        return "Мнения разделились почти поровну."
    if favorite_count * 100 < total * 65:
        return f"У {favorite_name} небольшой перевес."
    if favorite_count * 100 < total * 85:
        return f"{favorite_name} — явный фаворит прогнозов."
    return f"Почти все верят в {favorite_name}."


def series_result_insights(
    *,
    winner_count: int,
    exact_count: int,
    total: int,
) -> tuple[str, str]:
    _validate_count(winner_count, total=total)
    _validate_count(exact_count, total=total)

    if winner_count == 0:
        winner_text = "Победителя не выбрал никто."
    elif winner_count * 100 < total * 40:
        winner_text = "Получился неожиданный результат."
    elif winner_count * 100 < total * 60:
        winner_text = "Перед серией мнения были почти поровну."
    elif winner_count * 100 < total * 85:
        winner_text = "Большинство угадало победителя."
    else:
        winner_text = "Почти все угадали победителя."

    if exact_count == 0:
        exact_text = "Точный счёт не угадал никто."
    elif exact_count * 100 < total * 25:
        exact_text = "Точный счёт угадали немногие."
    else:
        exact_text = "Точных прогнозов оказалось много."
    return winner_text, exact_text


def champion_predictions_insights(
    votes: Sequence[tuple[str, int]],
    *,
    total: int,
) -> tuple[str, str]:
    positive_votes = tuple((name, count) for name, count in votes if count > 0)
    if not positive_votes:
        raise ValueError("Champion insights require at least one vote.")
    for _, count in positive_votes:
        _validate_count(count, total=total)
    if sum(count for _, count in positive_votes) != total:
        raise ValueError("Champion vote counts must add up to the total.")

    maximum = max(count for _, count in positive_votes)
    leaders = sorted(name for name, count in positive_votes if count == maximum)
    if len(leaders) > 1:
        favorite = f"У турнира несколько софаворитов: {_join_names(leaders)}."
    else:
        leader = leaders[0]
        if maximum * 100 < total * 30:
            favorite = f"Явного фаворита нет, чаще остальных выбирали {leader}."
        elif maximum * 100 < total * 60:
            favorite = f"Главный фаворит — {leader}."
        else:
            favorite = f"{leader} — доминирующий фаворит прогнозов."

    diversity = (
        "Прогнозы сосредоточены на нескольких командах."
        if len(positive_votes) <= 4
        else "Поддержку получил широкий круг претендентов."
    )
    return favorite, diversity


def champion_result_insight(
    *,
    champion_name: str,
    champion_count: int,
    votes: Sequence[tuple[str, int]],
    total: int,
) -> str:
    _validate_count(champion_count, total=total)
    if champion_count == 0:
        return "Чемпиона не выбрал никто."

    positive_counts = [count for _, count in votes if count > 0]
    if not positive_counts:
        raise ValueError("Champion result votes are inconsistent.")
    maximum = max(positive_counts)
    leader_count = sum(1 for count in positive_counts if count == maximum)
    if champion_count == maximum and leader_count == 1:
        return "Фаворит прогнозов оправдал ожидания."
    if champion_count == maximum:
        return "Победил один из софаворитов прогнозов."
    if champion_count * 100 >= total * 20:
        return "Победил заметный претендент, но не главный фаворит."
    return f"Победу {champion_name} предсказывали немногие."


def swiss_result_insights(
    *,
    total_points: int,
    prediction_count: int,
    maximum_points: int,
    perfect_count: int,
    least_supported_actual_count: int,
) -> tuple[str, str, str]:
    if prediction_count <= 0:
        raise ValueError("Swiss result insights require predictions.")
    if maximum_points <= 0:
        raise ValueError("Maximum points must be positive.")
    if total_points < 0:
        raise ValueError("Total points cannot be negative.")
    if total_points > prediction_count * maximum_points:
        raise ValueError("Total points cannot exceed the configured maximum.")
    _validate_count(perfect_count, total=prediction_count)
    _validate_count(least_supported_actual_count, total=prediction_count)

    denominator = prediction_count * maximum_points
    if total_points * 100 < denominator * 40:
        accuracy = "Швейцарский этап принёс много сюрпризов."
    elif total_points * 100 < denominator * 70:
        accuracy = "Большую часть проходов удалось предсказать."
    else:
        accuracy = "Прогнозы на швейцарский этап оказались очень точными."

    perfect = (
        "Идеальный прогноз не собрал никто."
        if perfect_count == 0
        else f"Идеальный прогноз собрали: {perfect_count}."
    )
    surprise = (
        "Хотя бы одна прошедшая команда была настоящим сюрпризом."
        if least_supported_actual_count * 100 < prediction_count * 25
        else "Явной неожиданности среди прошедших команд не было."
    )
    return accuracy, perfect, surprise


def contest_completion_insight(
    *,
    winner_points: int,
    runner_up_points: int,
    tiebreak_reason: str | None,
) -> str:
    margin = winner_points - runner_up_points
    if margin > 0:
        if margin == 1:
            return "Всё решил один балл."
        return f"Победа с отрывом в {margin} {_points_word(margin)}."

    messages = {
        "exact_score": "Решающими стали точные счета.",
        "goal_difference": "Решающими стали правильные разницы.",
        "outcome": "Решающими стали правильные исходы.",
        "drawn_advancing_team": ("Решающими стали прогнозы на прошедшие команды."),
        "champion": "Решающим стал прогноз на чемпиона.",
        "draw": "Все показатели совпали. Победитель определён жребием.",
    }
    try:
        return messages[tiebreak_reason]
    except KeyError as error:
        raise ValueError("A tied final requires a known tiebreak reason.") from error


def _validate_count(value: int, *, total: int) -> None:
    if total <= 0:
        raise ValueError("Total must be positive.")
    if value < 0 or value > total:
        raise ValueError("Count must be between zero and total.")


def _join_names(names: Sequence[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} и {names[1]}"
    return f"{', '.join(names[:-1])} и {names[-1]}"


def _points_word(value: int) -> str:
    if 11 <= value % 100 <= 14:
        return "баллов"
    if value % 10 == 1:
        return "балл"
    if value % 10 in (2, 3, 4):
        return "балла"
    return "баллов"
