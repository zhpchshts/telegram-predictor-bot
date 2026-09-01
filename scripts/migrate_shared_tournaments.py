from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.database import create_connection, initialize_database
from app.scoring_service import (
    recalculate_match_prediction_scores,
    recalculate_tie_prediction_scores,
)


TOURNAMENT_NAMES = {
    "world_cup_2026": "Чемпионат мира 2026",
    "the_international_2026": "The International 2026",
    "champions_league_2026_27": "Лига чемпионов 2026/27",
}


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    contest_count: int
    match_group_count: int
    local_match_count: int
    differing_time_groups: tuple[dict[str, object], ...]
    conflicts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "contest_count": self.contest_count,
            "match_group_count": self.match_group_count,
            "local_match_count": self.local_match_count,
            "differing_time_groups": list(self.differing_time_groups),
            "conflicts": list(self.conflicts),
        }


class SharedTournamentMigrationConflictError(RuntimeError):
    pass


def analyze_database(
    database_path: Path,
    *,
    now_utc: datetime | None = None,
) -> MigrationPlan:
    _resolve_now(now_utc)
    with create_connection(database_path) as connection:
        contest_rows = connection.execute(
            """
            SELECT id, template_key, name
            FROM contests
            WHERE is_active = 1
            ORDER BY template_key, id
            """
        ).fetchall()
        match_rows = _read_local_match_rows(connection)
        tournament_conflicts = _analyze_tournament_consistency(
            connection, contest_rows=contest_rows
        )
    groups = _group_matches(match_rows)
    contest_ids_by_template: dict[str, set[int]] = defaultdict(set)
    for contest_row in contest_rows:
        contest_ids_by_template[str(contest_row["template_key"])].add(
            int(contest_row["id"])
        )
    conflicts: list[str] = list(tournament_conflicts)
    differing_time_groups: list[dict[str, object]] = []
    for (template_key, team_a_id, team_b_id), rows in groups.items():
        contest_ids = [int(row["contest_id"]) for row in rows]
        if len(contest_ids) != len(set(contest_ids)):
            conflicts.append(
                f"{template_key}: в одном конкурсе найдено несколько матчей "
                f"пары {team_a_id}/{team_b_id}."
            )
        if set(contest_ids) != contest_ids_by_template[template_key]:
            conflicts.append(
                f"{template_key}: пара {team_a_id}/{team_b_id} присутствует "
                "не во всех конкурсах шаблона."
            )
        orientations = {
            (int(row["home_team_id"]), int(row["away_team_id"])) for row in rows
        }
        if len(orientations) != 1:
            conflicts.append(
                f"{template_key}: порядок команд пары {team_a_id}/{team_b_id} "
                "различается между конкурсами."
            )
        best_of_values = {row["best_of"] for row in rows}
        if len(best_of_values) != 1:
            conflicts.append(
                f"{template_key}: формат серии пары {team_a_id}/{team_b_id} "
                "различается между конкурсами."
            )
        result_values = {
            (
                int(row["home_score_final"]),
                int(row["away_score_final"]),
                int(row["advancing_team_id"]),
            )
            for row in rows
            if row["home_score_final"] is not None
            and row["away_score_final"] is not None
            and row["advancing_team_id"] is not None
        }
        incomplete_result_exists = any(
            (row["home_score_final"] is None) != (row["away_score_final"] is None)
            or (
                row["home_score_final"] is not None and row["advancing_team_id"] is None
            )
            for row in rows
        )
        if incomplete_result_exists or len(result_values) > 1:
            conflicts.append(
                f"{template_key}: результаты пары {team_a_id}/{team_b_id} "
                "не совпадают или заполнены не полностью."
            )
        cancelled_values = {str(row["status"]) == "cancelled" for row in rows}
        if len(cancelled_values) > 1:
            conflicts.append(
                f"{template_key}: статус отмены пары {team_a_id}/{team_b_id} "
                "различается между конкурсами."
            )
        times = sorted({str(row["starts_at_utc"]) for row in rows})
        if len(times) > 1:
            differing_time_groups.append(
                {
                    "template_key": template_key,
                    "team_ids": [team_a_id, team_b_id],
                    "local_match_ids": [int(row["match_id"]) for row in rows],
                    "starts_at_utc_values": times,
                }
            )
    return MigrationPlan(
        contest_count=len(contest_rows),
        match_group_count=len(groups),
        local_match_count=len(match_rows),
        differing_time_groups=tuple(differing_time_groups),
        conflicts=tuple(conflicts),
    )


def migrate_database(
    database_path: Path,
    *,
    actor_telegram_user_id: int,
    time_policy: str = "latest",
    now_utc: datetime | None = None,
) -> MigrationPlan:
    if actor_telegram_user_id <= 0:
        raise ValueError("actor_telegram_user_id must be positive.")
    if time_policy not in {"latest", "earliest"}:
        raise ValueError("time_policy must be latest or earliest.")
    plan = analyze_database(database_path, now_utc=now_utc)
    if plan.conflicts:
        raise SharedTournamentMigrationConflictError("\n".join(plan.conflicts))

    initialize_database(database_path)
    resolved_now = _resolve_now(now_utc)
    with create_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing_links = connection.execute(
            "SELECT COUNT(*) FROM contest_shared_tournaments"
        ).fetchone()[0]
        existing_shared_matches = connection.execute(
            "SELECT COUNT(*) FROM shared_matches"
        ).fetchone()[0]
        if existing_links or existing_shared_matches:
            raise SharedTournamentMigrationConflictError(
                "Shared tournament data already exists; migration was not applied."
            )
        contest_rows = connection.execute(
            """
            SELECT id, template_key
            FROM contests
            WHERE is_active = 1
            ORDER BY template_key, id
            """
        ).fetchall()
        contests_by_template: dict[str, list[int]] = defaultdict(list)
        for row in contest_rows:
            contests_by_template[str(row["template_key"])].append(int(row["id"]))

        tournament_ids: dict[str, int] = {}
        for template_key, contest_ids in contests_by_template.items():
            tournament_id = int(
                connection.execute(
                    """
                    INSERT INTO shared_tournaments (
                        name, template_key, created_by_telegram_user_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        TOURNAMENT_NAMES.get(template_key, template_key),
                        template_key,
                        actor_telegram_user_id,
                    ),
                ).lastrowid
            )
            tournament_ids[template_key] = tournament_id
            common_settings = _read_common_tournament_settings(
                connection, contest_ids=contest_ids
            )
            connection.execute(
                """
                INSERT INTO shared_tournament_settings (
                    shared_tournament_id,
                    champion_prediction_enabled,
                    champion_prediction_deadline_at,
                    champion_prediction_points,
                    champion_team_id,
                    swiss_stage_prediction_enabled,
                    swiss_stage_prediction_deadline_at,
                    swiss_direct_qualifier_count,
                    swiss_elimination_qualifier_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament_id,
                    common_settings["champion_prediction_enabled"],
                    common_settings["champion_prediction_deadline_at"],
                    common_settings["champion_prediction_points"],
                    common_settings["champion_team_id"],
                    common_settings["swiss_stage_prediction_enabled"],
                    common_settings["swiss_stage_prediction_deadline_at"],
                    common_settings["swiss_direct_qualifier_count"],
                    common_settings["swiss_elimination_qualifier_count"],
                ),
            )
            connection.executemany(
                """
                INSERT INTO contest_shared_tournaments (
                    contest_id, shared_tournament_id
                )
                VALUES (?, ?)
                """,
                [(contest_id, tournament_id) for contest_id in contest_ids],
            )
            team_rows = connection.execute(
                f"""
                SELECT DISTINCT team_id
                FROM contest_teams
                WHERE contest_id IN ({",".join("?" for _ in contest_ids)})
                ORDER BY team_id
                """,
                tuple(contest_ids),
            ).fetchall()
            connection.executemany(
                """
                INSERT INTO shared_tournament_teams (
                    shared_tournament_id, team_id, position
                )
                VALUES (?, ?, ?)
                """,
                [
                    (tournament_id, int(row["team_id"]), position)
                    for position, row in enumerate(team_rows)
                ],
            )
            connection.executemany(
                """
                INSERT INTO shared_swiss_stage_result_selections (
                    shared_tournament_id, team_id, category
                ) VALUES (?, ?, ?)
                """,
                [
                    (tournament_id, team_id, category)
                    for team_id, category in common_settings[
                        "swiss_stage_result_selections"
                    ]
                ],
            )
            for contest_id in contest_ids:
                local_settings = _read_tournament_settings(
                    connection, contest_id=contest_id
                )
                local_champion_deadline = local_settings[
                    "champion_prediction_deadline_at"
                ]
                if (
                    local_champion_deadline is None
                    or _parse_time(str(local_champion_deadline)) > resolved_now
                ):
                    connection.execute(
                        """
                        UPDATE contests
                        SET champion_prediction_enabled = ?,
                            champion_prediction_deadline_at = ?,
                            champion_team_id = ?
                        WHERE id = ?
                        """,
                        (
                            common_settings["champion_prediction_enabled"],
                            common_settings["champion_prediction_deadline_at"],
                            common_settings["champion_team_id"],
                            contest_id,
                        ),
                    )
                elif common_settings["champion_team_id"] is not None:
                    connection.execute(
                        "UPDATE contests SET champion_team_id = ? WHERE id = ?",
                        (common_settings["champion_team_id"], contest_id),
                    )

                local_swiss_deadline = local_settings[
                    "swiss_stage_prediction_deadline_at"
                ]
                if (
                    local_swiss_deadline is None
                    or _parse_time(str(local_swiss_deadline)) > resolved_now
                ):
                    connection.execute(
                        """
                        INSERT INTO swiss_stage_prediction_settings (
                            contest_id, enabled, deadline_at,
                            direct_qualifier_count, elimination_qualifier_count
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(contest_id) DO UPDATE SET
                            enabled = excluded.enabled,
                            deadline_at = excluded.deadline_at,
                            direct_qualifier_count = excluded.direct_qualifier_count,
                            elimination_qualifier_count =
                                excluded.elimination_qualifier_count,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            contest_id,
                            common_settings["swiss_stage_prediction_enabled"],
                            common_settings["swiss_stage_prediction_deadline_at"],
                            common_settings["swiss_direct_qualifier_count"],
                            common_settings["swiss_elimination_qualifier_count"],
                        ),
                    )
                swiss_result = common_settings["swiss_stage_result_selections"]
                if swiss_result:
                    connection.execute(
                        """
                        INSERT INTO swiss_stage_results (contest_id) VALUES (?)
                        ON CONFLICT(contest_id) DO UPDATE SET
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (contest_id,),
                    )
                    connection.execute(
                        "DELETE FROM swiss_stage_result_selections WHERE contest_id = ?",
                        (contest_id,),
                    )
                    connection.executemany(
                        """
                        INSERT INTO swiss_stage_result_selections (
                            contest_id, team_id, category
                        ) VALUES (?, ?, ?)
                        """,
                        [
                            (contest_id, team_id, category)
                            for team_id, category in swiss_result
                        ],
                    )
            connection.execute(
                """
                INSERT INTO shared_tournament_events (
                    shared_tournament_id, actor_telegram_user_id,
                    event_type, after_state, metadata
                )
                VALUES (?, ?, 'shared_tournament.migrated', ?, ?)
                """,
                (
                    tournament_id,
                    actor_telegram_user_id,
                    json.dumps(
                        {"template_key": template_key},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    json.dumps(
                        {"contest_ids": contest_ids},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )

        groups = _group_matches(_read_local_match_rows(connection))
        for (template_key, _team_a_id, _team_b_id), rows in groups.items():
            canonical = rows[0]
            times = [str(row["starts_at_utc"]) for row in rows]
            canonical_time = max(times) if time_policy == "latest" else min(times)
            result_rows = [row for row in rows if row["home_score_final"] is not None]
            result_row = result_rows[0] if result_rows else None
            status = _canonical_status(
                rows,
                canonical_time=canonical_time,
                result_exists=result_row is not None,
                now_utc=resolved_now,
            )
            shared_match_id = int(
                connection.execute(
                    """
                    INSERT INTO shared_matches (
                        shared_tournament_id, home_team_id, away_team_id,
                        starts_at_utc, best_of, status, home_score_final,
                        away_score_final, advancing_team_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tournament_ids[template_key],
                        int(canonical["home_team_id"]),
                        int(canonical["away_team_id"]),
                        canonical_time,
                        canonical["best_of"],
                        status,
                        result_row["home_score_final"] if result_row else None,
                        result_row["away_score_final"] if result_row else None,
                        result_row["advancing_team_id"] if result_row else None,
                    ),
                ).lastrowid
            )
            for row in rows:
                match_id = int(row["match_id"])
                tie_id = int(row["tie_id"])
                local_time = str(row["starts_at_utc"])
                local_deadline_elapsed = _parse_time(local_time) <= resolved_now
                migrated_time = local_time if local_deadline_elapsed else canonical_time
                local_status = status
                if not result_row and status == "scheduled" and local_deadline_elapsed:
                    local_status = "started"
                connection.execute(
                    """
                    INSERT INTO shared_match_links (
                        shared_match_id, match_id, contest_id
                    )
                    VALUES (?, ?, ?)
                    """,
                    (shared_match_id, match_id, int(row["contest_id"])),
                )
                connection.execute(
                    """
                    UPDATE matches
                    SET starts_at_utc = ?, status = ?, home_score_final = ?,
                        away_score_final = ?
                    WHERE id = ?
                    """,
                    (
                        migrated_time,
                        local_status,
                        result_row["home_score_final"] if result_row else None,
                        result_row["away_score_final"] if result_row else None,
                        match_id,
                    ),
                )
                connection.execute(
                    "UPDATE ties SET advancing_team_id = ? WHERE id = ?",
                    (
                        result_row["advancing_team_id"] if result_row else None,
                        tie_id,
                    ),
                )
                recalculate_match_prediction_scores(connection, match_id=match_id)
                recalculate_tie_prediction_scores(connection, tie_id=tie_id)
            connection.execute(
                """
                INSERT INTO shared_tournament_events (
                    shared_tournament_id, shared_match_id,
                    actor_telegram_user_id, event_type, after_state, metadata
                )
                VALUES (?, ?, ?, 'shared_match.migrated', ?, ?)
                """,
                (
                    tournament_ids[template_key],
                    shared_match_id,
                    actor_telegram_user_id,
                    json.dumps(
                        {
                            "starts_at_utc": canonical_time,
                            "status": status,
                            "home_score_final": (
                                result_row["home_score_final"] if result_row else None
                            ),
                            "away_score_final": (
                                result_row["away_score_final"] if result_row else None
                            ),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            "local_match_ids": [int(row["match_id"]) for row in rows],
                            "time_policy": time_policy,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Migration introduced foreign key violations.")
        connection.commit()
    return plan


def backup_database(source_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(backup_path) as target:
        source.backup(target)


def _analyze_tournament_consistency(
    connection: sqlite3.Connection, *, contest_rows: list[sqlite3.Row]
) -> list[str]:
    contests_by_template: dict[str, list[int]] = defaultdict(list)
    for row in contest_rows:
        contests_by_template[str(row["template_key"])].append(int(row["id"]))
    conflicts: list[str] = []
    for template_key, contest_ids in contests_by_template.items():
        team_sets = {
            frozenset(
                int(row["team_id"])
                for row in connection.execute(
                    """
                    SELECT team_id FROM contest_teams
                    WHERE contest_id = ? ORDER BY position, team_id
                    """,
                    (contest_id,),
                ).fetchall()
            )
            for contest_id in contest_ids
        }
        if len(team_sets) > 1:
            conflicts.append(
                f"{template_key}: состав команд различается между конкурсами."
            )
        snapshots = [
            _read_tournament_settings(connection, contest_id=contest_id)
            for contest_id in contest_ids
        ]
        if len({item["champion_prediction_enabled"] for item in snapshots}) > 1:
            conflicts.append(
                f"{template_key}: состояние прогноза на чемпиона различается."
            )
        champion_results = {
            item["champion_team_id"]
            for item in snapshots
            if item["champion_team_id"] is not None
        }
        if len(champion_results) > 1:
            conflicts.append(f"{template_key}: фактические чемпионы различаются.")
        if len({item["swiss_stage_prediction_enabled"] for item in snapshots}) > 1:
            conflicts.append(
                f"{template_key}: состояние прогноза на швейцарский этап различается."
            )
        if len({item["swiss_direct_qualifier_count"] for item in snapshots}) > 1:
            conflicts.append(f"{template_key}: лимиты прямого прохода различаются.")
        if len({item["swiss_elimination_qualifier_count"] for item in snapshots}) > 1:
            conflicts.append(f"{template_key}: лимиты второй категории различаются.")
        swiss_results = {
            item["swiss_stage_result_selections"]
            for item in snapshots
            if item["swiss_stage_result_selections"]
        }
        if len(swiss_results) > 1:
            conflicts.append(f"{template_key}: итоги швейцарского этапа различаются.")
    return conflicts


def _read_common_tournament_settings(
    connection: sqlite3.Connection, *, contest_ids: list[int]
) -> dict[str, object]:
    if not contest_ids:
        raise RuntimeError("No contests found for shared tournament migration.")
    snapshots = [
        _read_tournament_settings(connection, contest_id=contest_id)
        for contest_id in contest_ids
    ]
    first = snapshots[0]
    champion_deadlines = [
        str(item["champion_prediction_deadline_at"])
        for item in snapshots
        if item["champion_prediction_deadline_at"] is not None
    ]
    champion_points = [int(item["champion_prediction_points"]) for item in snapshots]
    champion_results = [
        item["champion_team_id"]
        for item in snapshots
        if item["champion_team_id"] is not None
    ]
    swiss_deadlines = [
        str(item["swiss_stage_prediction_deadline_at"])
        for item in snapshots
        if item["swiss_stage_prediction_deadline_at"] is not None
    ]
    swiss_results = [
        item["swiss_stage_result_selections"]
        for item in snapshots
        if item["swiss_stage_result_selections"]
    ]
    return {
        "champion_prediction_enabled": first["champion_prediction_enabled"],
        "champion_prediction_deadline_at": (
            max(champion_deadlines) if champion_deadlines else None
        ),
        "champion_prediction_points": max(
            set(champion_points),
            key=lambda value: (champion_points.count(value), value),
        ),
        "champion_team_id": champion_results[0] if champion_results else None,
        "swiss_stage_prediction_enabled": first["swiss_stage_prediction_enabled"],
        "swiss_stage_prediction_deadline_at": (
            max(swiss_deadlines) if swiss_deadlines else None
        ),
        "swiss_direct_qualifier_count": first["swiss_direct_qualifier_count"],
        "swiss_elimination_qualifier_count": first["swiss_elimination_qualifier_count"],
        "swiss_stage_result_selections": swiss_results[0] if swiss_results else (),
    }


def _read_tournament_settings(
    connection: sqlite3.Connection, *, contest_id: int
) -> dict[str, object]:
    contest = connection.execute(
        """
        SELECT champion_prediction_enabled, champion_prediction_deadline_at,
               champion_prediction_points, champion_team_id
        FROM contests WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if contest is None:
        raise RuntimeError("Contest disappeared during migration analysis.")
    swiss = connection.execute(
        """
        SELECT enabled, deadline_at, direct_qualifier_count,
               elimination_qualifier_count
        FROM swiss_stage_prediction_settings WHERE contest_id = ?
        """,
        (contest_id,),
    ).fetchone()
    result_rows = connection.execute(
        """
        SELECT team_id, category FROM swiss_stage_result_selections
        WHERE contest_id = ? ORDER BY category, team_id
        """,
        (contest_id,),
    ).fetchall()
    return {
        "contest_id": contest_id,
        "champion_prediction_enabled": int(contest["champion_prediction_enabled"]),
        "champion_prediction_deadline_at": contest["champion_prediction_deadline_at"],
        "champion_prediction_points": int(contest["champion_prediction_points"]),
        "champion_team_id": contest["champion_team_id"],
        "swiss_stage_prediction_enabled": int(swiss["enabled"]) if swiss else 0,
        "swiss_stage_prediction_deadline_at": swiss["deadline_at"] if swiss else None,
        "swiss_direct_qualifier_count": (
            int(swiss["direct_qualifier_count"]) if swiss else 3
        ),
        "swiss_elimination_qualifier_count": (
            int(swiss["elimination_qualifier_count"]) if swiss else 5
        ),
        "swiss_stage_result_selections": tuple(
            (int(row["team_id"]), str(row["category"])) for row in result_rows
        ),
    }


def _read_local_match_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            contests.template_key,
            contests.id AS contest_id,
            matches.id AS match_id,
            matches.tie_id,
            matches.home_team_id,
            matches.away_team_id,
            matches.starts_at_utc,
            matches.best_of,
            matches.status,
            matches.home_score_final,
            matches.away_score_final,
            ties.advancing_team_id
        FROM matches
        JOIN ties ON ties.id = matches.tie_id
        JOIN stages ON stages.id = matches.stage_id
        JOIN competitions ON competitions.id = stages.competition_id
        JOIN contests ON contests.id = competitions.contest_id
        WHERE contests.is_active = 1
        ORDER BY contests.template_key, matches.id
        """
    ).fetchall()


def _group_matches(rows: list[sqlite3.Row]):
    groups: dict[tuple[str, int, int], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        home_team_id = int(row["home_team_id"])
        away_team_id = int(row["away_team_id"])
        groups[
            (
                str(row["template_key"]),
                min(home_team_id, away_team_id),
                max(home_team_id, away_team_id),
            )
        ].append(row)
    return groups


def _canonical_status(
    rows: list[sqlite3.Row],
    *,
    canonical_time: str,
    result_exists: bool,
    now_utc: datetime,
) -> str:
    if result_exists:
        return "finished"
    if all(str(row["status"]) == "cancelled" for row in rows):
        return "cancelled"
    return "started" if _parse_time(canonical_time) <= now_utc else "scheduled"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"Stored match time has no timezone: {value}")
    return parsed.astimezone(timezone.utc)


def _resolve_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_utc must include a timezone.")
    return value.astimezone(timezone.utc)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or migrate existing contests to shared tournaments."
    )
    parser.add_argument("database_path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-telegram-user-id", type=int)
    parser.add_argument(
        "--time-policy", choices=("latest", "earliest"), default="latest"
    )
    parser.add_argument("--backup-path", type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = _parse_arguments()
    plan = analyze_database(arguments.database_path)
    print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
    if not arguments.apply:
        return
    if arguments.actor_telegram_user_id is None:
        raise SystemExit("--actor-telegram-user-id is required with --apply")
    if plan.conflicts:
        raise SystemExit("Migration conflicts must be resolved before --apply")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = arguments.backup_path or arguments.database_path.with_name(
        f"{arguments.database_path.name}.{timestamp}.bak"
    )
    backup_database(arguments.database_path, backup_path)
    migrate_database(
        arguments.database_path,
        actor_telegram_user_id=arguments.actor_telegram_user_id,
        time_policy=arguments.time_policy,
    )
    print(f"Backup: {backup_path}")
    print("Migration completed.")


if __name__ == "__main__":
    main()
