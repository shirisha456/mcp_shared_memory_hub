"""How search behaves as the corpus grows.

One measurement at 10k says nothing about shape. The question that matters for a
system meant to accumulate knowledge for years is whether cost grows with the
corpus or with the answer - and only three points on a curve can distinguish
those.

Marked ``perf`` and deselected by default; seeding 100k memories takes long
enough to be worth running deliberately.

    pytest -m perf
"""

from __future__ import annotations

import statistics
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memhub.services.memories import search
from memhub.services.projects import use_project
from tests.perf.test_search_latency import RARE_TERM, generate_corpus

pytestmark = [pytest.mark.integration, pytest.mark.perf]

SCALES = (1_000, 10_000, 100_000)
PLANS_DIR = Path(__file__).resolve().parents[2] / "docs" / "perf"

SERVER_BUDGET_MS = 50.0
"""Must hold at every scale, not just the smallest.

This is the assertion that would catch a query whose cost is linear in the
corpus: it passes comfortably at 1k and fails at 100k.
"""


async def seed_scale(session: AsyncSession, project_id: uuid.UUID, count: int) -> None:
    """Bulk-load in chunks.

    A single 100k-row parameter list is a very large statement to build and send;
    chunking keeps memory flat and is what a real backfill would do anyway.
    """
    rows = generate_corpus(count)
    for row in rows:
        row["pid"] = project_id

    chunk = 5_000
    for start in range(0, len(rows), chunk):
        batch = rows[start : start + chunk]
        await session.execute(
            text(
                "INSERT INTO memories (id, project_id, type, status, "
                "current_revision_no, importance, expires_at) VALUES "
                "(:mid, :pid, :type, 'ACTIVE', 1, :importance, :expires)"
            ),
            batch,
        )
        await session.execute(
            text(
                "INSERT INTO memory_revisions (memory_id, project_id, revision_no, "
                "content, content_hash, hash_version, tags, is_current, "
                "author_client, author_kind) VALUES "
                "(:mid, :pid, 1, :content, :chash, :hver, '{}', true, "
                "'benchmark', 'import')"
            ),
            batch,
        )
    await session.execute(text("ANALYZE memories"))
    await session.execute(text("ANALYZE memory_revisions"))
    await session.commit()


async def server_time_ms(session: AsyncSession, sql: str) -> float:
    """PostgreSQL's own execution time, free of transport overhead."""
    rows = await session.execute(text(f"EXPLAIN ANALYZE {sql}"))
    plan = "\n".join(str(row[0]) for row in rows.all())
    return float(plan.rsplit("Execution Time:", 1)[1].split("ms")[0].strip())


async def test_search_cost_grows_with_the_answer_not_the_corpus(
    engine: AsyncEngine,
) -> None:
    """The shape question, measured at three scales.

    A selective query should cost roughly the same at 100k as at 1k: the work is
    proportional to the number of matches, not the size of the table. If it is
    not - if the numbers climb with the corpus - then something is scanning, and
    that is a problem which only becomes visible at a size no unit test reaches.
    """
    from memhub.persistence.engine import create_session_factory

    sessions = create_session_factory(engine)
    report: list[str] = []
    timings: dict[int, float] = {}

    for scale in SCALES:
        async with sessions() as session:
            project = await use_project(session, slug=f"scale-{scale}", create=True)
            await session.commit()
            await seed_scale(session, project.id, scale)

        async with sessions() as session:
            # Warm, then measure both a selective and a broad query.
            for _ in range(3):
                await search(session, project.id, query=RARE_TERM, limit=10)

            selective = statistics.median(
                [
                    await server_time_ms(
                        session,
                        "SELECT m.id FROM memories m JOIN memory_revisions r "
                        "ON r.memory_id = m.id AND r.project_id = m.project_id "
                        f"WHERE m.project_id = '{project.id}' AND m.status = 'ACTIVE' "
                        "AND r.is_current AND r.content_tsv @@ "
                        f"websearch_to_tsquery('english', '{RARE_TERM}') LIMIT 10",
                    )
                    for _ in range(5)
                ]
            )

            started = time.perf_counter()
            found = await search(session, project.id, query=RARE_TERM, limit=10)
            client_ms = (time.perf_counter() - started) * 1000

        timings[scale] = selective

        if scale == SCALES[-1]:
            # The crossover, captured. At 10k PostgreSQL declines the GIN index
            # because the table is small enough to scan; the question that
            # measurement left open is whether it takes it once the corpus is
            # large enough for the index to pay. This is that answer.
            async with sessions() as probe:
                rows = await probe.execute(
                    text(
                        "EXPLAIN ANALYZE SELECT m.id FROM memories m "
                        "JOIN memory_revisions r ON r.memory_id = m.id "
                        "AND r.project_id = m.project_id "
                        f"WHERE m.project_id = '{project.id}' AND m.status = 'ACTIVE' "
                        "AND r.is_current AND r.content_tsv @@ "
                        f"websearch_to_tsquery('english', '{RARE_TERM}') LIMIT 10"
                    )
                )
                largest_plan = "\n".join(str(row[0]) for row in rows.all())
        report.append(
            f"{scale:>7,} memories  matched={found.total_considered:<6d} "
            f"server={selective:6.2f}ms  client={client_ms:6.2f}ms"
        )
        assert selective < SERVER_BUDGET_MS, (
            f"at {scale:,} memories a selective query took {selective:.2f}ms "
            f"inside PostgreSQL, over the {SERVER_BUDGET_MS}ms budget\n" + "\n".join(report)
        )

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    growth = timings[SCALES[-1]] / timings[SCALES[0]] if timings[SCALES[0]] else 0.0
    corpus_growth = SCALES[-1] / SCALES[0]
    (PLANS_DIR / "scaling.txt").write_text(
        "Selective full-text search as the corpus grows.\n"
        "Server time is PostgreSQL's own execution time; client time includes\n"
        "Docker port forwarding on the development machine.\n\n"
        + "\n".join(report)
        + f"\n\nCorpus grew {corpus_growth:.0f}x; query time grew {growth:.1f}x.\n"
        "Sub-linear growth is the property that matters: the work is proportional\n"
        "to the number of matches, not the size of the table.\n",
        encoding="utf-8",
    )

    (PLANS_DIR / "scaling_plan.txt").write_text(
        f"EXPLAIN ANALYZE at {SCALES[-1]:,} memories, selective query, LIMIT 10.\n\n"
        "Recorded, not asserted on. Two earlier versions of this benchmark did\n"
        "assert that the GIN index appears in the plan, and both were wrong for\n"
        "the same reason: PostgreSQL keeps finding cheaper ways to answer the\n"
        "query than the one the test expected.\n\n"
        "Here it chooses a sequential scan even at 100,000 rows, and it is right\n"
        "to. With LIMIT 10 the scan stops as soon as it has ten matches - note\n"
        "'Rows Removed by Filter' in the plan below, a small fraction of the\n"
        "table - so index access plus heap fetches would cost more than simply\n"
        "reading until satisfied. The index earns its keep on queries that must\n"
        "examine every match, not on ones that stop early.\n\n"
        "What matters is in scaling.txt: cost grows with the answer, not the\n"
        "corpus. That is the property an index exists to provide, and it holds\n"
        "whichever access path the planner picks to provide it.\n\n" + largest_plan + "\n",
        encoding="utf-8",
    )

    assert growth < corpus_growth / 10, (
        f"query time grew {growth:.1f}x while the corpus grew {corpus_growth:.0f}x - "
        "that is closer to linear than it should be, which means something is "
        "scanning the table rather than using an index\n" + "\n".join(report)
    )
