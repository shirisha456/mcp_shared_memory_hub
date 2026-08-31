"""Retrieval quality, measured and gated.

Runs every graded query against the real retriever over a 200-memory corpus,
computes nDCG, recall, precision and stale inclusion, writes the results to
``docs/eval/results.md``, and fails if quality has dropped against the committed
baseline in ``eval/dataset/baseline.json``.

**Why this exists before semantic search rather than after.** Building the
retriever first and the measurement second means choosing the metric that agrees
with the retriever you already built. The full-text numbers recorded here are the
number hybrid retrieval will have to beat, written down before there is any
incentive to flatter it.

Regenerate the baseline deliberately, and read the diff:

    MEMHUB_UPDATE_EVAL_BASELINE=1 pytest tests/eval
"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memhub.evaluation.harness import Report, render_markdown, score_query
from memhub.services.memories import search
from tests.eval.dataset import (
    BASELINE,
    HISTORY,
    RESULTS,
    SeededCorpus,
    load_queries,
    seed_corpus,
)

pytestmark = [pytest.mark.integration, pytest.mark.eval]

K = 10

# How far a metric may fall before the build fails. Small but not zero: the
# ranking depends on a recency prior computed against the database clock, so
# scores move fractionally between runs. Zero tolerance would make this flaky;
# a large tolerance would let a real regression through unnoticed.
TOLERANCE = 0.02

STRATEGY = "full-text with any-term fallback (ts_rank_cd + importance, recency, type priors)"


@pytest.fixture(scope="module")
async def corpus(engine: AsyncEngine) -> SeededCorpus:
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        seeded = await seed_corpus(session)
        await session.commit()
        return seeded


async def run_evaluation(session: AsyncSession, corpus: SeededCorpus) -> Report:
    results = []
    for graded in load_queries():
        found = await search(session, corpus.project_id, query=graded.query, limit=K)
        ranked = [
            corpus.to_eval_id[memory.memory_id]
            for memory in found.memories
            if memory.memory_id in corpus.to_eval_id
        ]
        results.append(score_query(graded, ranked, k=K))
    return Report(k=K, results=tuple(results))


async def test_no_query_returns_a_retired_or_foreign_memory(
    committing_session: AsyncSession, corpus: SeededCorpus
) -> None:
    """The correctness metric, asserted at exactly zero.

    Several queries in the dataset match a *superseded* memory better than its
    replacement - q02 asks for "redis" when the retired memory is about Redis and
    the current one mentions it only to say it was removed. A similarity-only
    system ranks the retired memory first on every one of them.

    Separate from the quality gate below and stricter, because this is not a
    trade-off. A change that raised nDCG while leaking one retired memory would
    be a regression.
    """
    report = await run_evaluation(committing_session, corpus)

    assert report.stale_inclusion_rate == 0.0, (
        "retrieval returned memories that must never be returned:\n"
        + "\n".join(
            f"  {r.query.id} {r.query.query!r} -> {sorted(set(r.ranked) & set(r.query.forbidden))}"
            for r in report.leaks
        )
    )


async def test_quality_has_not_regressed(
    committing_session: AsyncSession, corpus: SeededCorpus
) -> None:
    """The regression gate.

    Without a committed baseline an evaluation harness is a one-off
    demonstration: it tells you a number today and notices nothing tomorrow. The
    comparison is what turns it into engineering.
    """
    report = await run_evaluation(committing_session, corpus)
    current = report.as_dict()

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        render_markdown(
            report,
            strategy=STRATEGY,
            corpus_size=corpus.size,
            history=json.loads(HISTORY.read_text(encoding="utf-8")),
        ),
        encoding="utf-8",
    )

    if os.environ.get("MEMHUB_UPDATE_EVAL_BASELINE") == "1":
        BASELINE.write_text(
            json.dumps({"strategy": STRATEGY, **current}, indent=2) + "\n", encoding="utf-8"
        )
        pytest.skip(f"baseline rewritten at {BASELINE} - review the diff before committing")

    if not BASELINE.is_file():
        BASELINE.write_text(
            json.dumps({"strategy": STRATEGY, **current}, indent=2) + "\n", encoding="utf-8"
        )
        pytest.fail(f"no baseline existed; wrote one at {BASELINE}. Review and commit it.")

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for metric in ("ndcg", "recall", "precision"):
        assert current[metric] >= baseline[metric] - TOLERANCE, (
            f"{metric} fell from {baseline[metric]} to {current[metric]} "
            f"(tolerance {TOLERANCE}).\n"
            f"If this change is a deliberate trade-off, regenerate the baseline "
            f"with MEMHUB_UPDATE_EVAL_BASELINE=1 and explain it in the commit."
        )


async def test_the_dataset_is_internally_consistent(
    committing_session: AsyncSession, corpus: SeededCorpus
) -> None:
    """Guard against judgments that reference memories the corpus does not have.

    A typo'd id in queries.yaml would silently lower every metric - the memory
    can never be returned because it does not exist - and would look like a
    retrieval problem rather than a dataset problem.
    """
    known = set(corpus.by_eval_id)
    for graded in load_queries():
        unknown_relevant = set(graded.relevant) - known
        unknown_forbidden = set(graded.forbidden) - known
        assert not unknown_relevant, f"{graded.id} judges unknown memories: {unknown_relevant}"
        assert not unknown_forbidden, f"{graded.id} forbids unknown memories: {unknown_forbidden}"


async def test_forbidden_memories_are_genuinely_unreachable(
    committing_session: AsyncSession, corpus: SeededCorpus
) -> None:
    """The forbidden lists must name memories that really were retired.

    If a `forbidden` entry pointed at a memory that is still active, the stale
    inclusion metric would be measuring nothing - it would pass because the
    retrieval never happened to rank it, not because suppression works.
    """
    from sqlalchemy import text

    for graded in load_queries():
        for name in graded.forbidden:
            memory_id = corpus.by_eval_id[name]
            status = (
                await committing_session.execute(
                    text("SELECT status FROM memories WHERE id = :id"), {"id": memory_id}
                )
            ).scalar_one()
            if name.startswith("x"):
                # Cross-project traps are active, just in a different project.
                assert status == "ACTIVE"
            else:
                assert status == "SUPERSEDED", (
                    f"{name} is listed as forbidden for {graded.id} but its status "
                    f"is {status} - the dataset expects it to have been retired"
                )
