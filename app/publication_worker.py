from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sqlite3

from app.contest_publications import render_publication_messages
from app.publication_delivery import (
    ClaimLostError,
    PermanentDeliveryError,
    TelegramPublicationClient,
    TemporaryDeliveryError,
    deliver_publication,
)
from app.publication_outbox import (
    StalePublicationRevision,
    claim_next_publication,
    finish_publication_failure,
    finish_publication_success,
    prepare_scheduled_reconciliation,
)


LOGGER = logging.getLogger(__name__)
PUBLICATION_POLL_INTERVAL_SECONDS = 15.0


async def run_contest_publication_worker(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    poll_interval_seconds: float = PUBLICATION_POLL_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            await process_due_contest_publications(
                bot=bot,
                database_path=database_path,
            )
        except Exception:
            LOGGER.exception("Could not process contest publications.")
        await asyncio.sleep(poll_interval_seconds)


async def process_due_contest_publications(
    *,
    bot: TelegramPublicationClient,
    database_path: Path,
    max_publications: int = 100,
) -> int:
    processed_count = 0
    for _ in range(max_publications):
        claimed = claim_next_publication(database_path=database_path)
        if claimed is None:
            break
        publication = claimed

        try:
            prepared_publication = prepare_scheduled_reconciliation(
                database_path=database_path,
                publication=claimed,
            )
            if prepared_publication is None:
                continue
            publication = prepared_publication

            desired_messages = render_publication_messages(
                database_path=database_path,
                publication=publication,
            )
            await deliver_publication(
                bot=bot,
                database_path=database_path,
                publication=publication,
                desired_messages=desired_messages,
            )
        except ClaimLostError:
            LOGGER.warning("Lost claim for contest publication %s.", claimed.id)
            continue
        except StalePublicationRevision:
            finish_publication_success(
                database_path=database_path,
                publication=publication,
                status=(
                    "withdrawn"
                    if publication.desired_action == "withdraw"
                    else "published"
                ),
            )
            continue
        except TemporaryDeliveryError as error:
            finish_publication_failure(
                database_path=database_path,
                publication=publication,
                error=str(error),
                permanent=False,
                retry_after_seconds=error.retry_after_seconds,
            )
            continue
        except PermanentDeliveryError as error:
            finish_publication_failure(
                database_path=database_path,
                publication=publication,
                error=str(error),
                permanent=True,
            )
            continue
        except sqlite3.OperationalError as error:
            finish_publication_failure(
                database_path=database_path,
                publication=publication,
                error=str(error),
                permanent=False,
            )
            continue
        except Exception as error:
            LOGGER.exception("Contest publication %s failed.", claimed.id)
            finish_publication_failure(
                database_path=database_path,
                publication=publication,
                error=str(error),
                permanent=True,
            )
            continue

        finish_publication_success(
            database_path=database_path,
            publication=publication,
            status=(
                "withdrawn" if publication.desired_action == "withdraw" else "published"
            ),
        )
        processed_count += 1

    return processed_count
