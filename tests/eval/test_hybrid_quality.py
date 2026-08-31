"""Does hybrid retrieval actually beat full text?

Measured against the same corpus and the same judgments as the full-text
baseline, which were written before either retriever existed.

**Why this is marked and not run by default.** It needs a real embedding model,
which means a ~50MB download and CPU inference. CI runs the full-text evaluation
and the hermetic outbox tests with the deterministic fake; the numbers committed
to ``eval/dataset/history.json`` come from running this locally.

That split is deliberate and worth being explicit about: the fake embedder cannot
measure quality. Two sentences meaning the same thing get unrelated vectors, so
an nDCG computed against it would be noise dressed up as a number. A fake that
*looked* semantic would be worse than useless.

    pip install -e ".[local-embeddings]"
    pytest tests/eval/test_hybrid_quality.py -m real_embeddings
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from memhub.embeddings.local import LocalEmbedder
from memhub.embeddings.worker import EmbeddingWorker
from memhub.evaluation.harness import Report, score_query
from memhub.persistence.engine import create_session_factory
from memhub.services.retrieval import hybrid_search
from tests.eval.dataset import BASELINE, SeededCorpus, load_queries, seed_corpus

pytestmark = [pytest.mark.integration, pytest.mark.eval, pytest.mark.real_embeddings]

K = 10


@pytest.fixture(scope="module")
def embedder() -> LocalEmbedder:
    return LocalEmbedder()


@pytest.fixture(scope="module")
async def embedded_corpus(engine: AsyncEngine, embedder: LocalEmbedder) -> SeededCorpus:
    """Seed the corpus and drain the outbox.

    Note that embedding happens *after* the writes, through the real worker,
    rather than inline. That is not incidental to the test - it is the same path
    production uses, so this also exercises the outbox end to end at 200 rows.
    """
    factory = create_session_factory(engine)
    async with factory() as session:
        corpus = await seed_corpus(session, embedding_model=embedder.model_name)
        await session.commit()

    outcome = await EmbeddingWorker(factory, embedder, batch_size=32).drain()
    assert outcome.failed == 0, f"embedding failed: {outcome}"
    return corpus


async def run_hybrid(
    session: AsyncSession, corpus: SeededCorpus, embedder: LocalEmbedder
) -> Report:
    results = []
    for graded in load_queries():
        found = await hybrid_search(
            session, corpus.project_id, query=graded.query, embedder=embedder, limit=K
        )
        ranked = [
            corpus.to_eval_id[memory.memory_id]
            for memory in found.memories
            if memory.memory_id in corpus.to_eval_id
        ]
        results.append(score_query(graded, ranked, k=K))
    return Report(k=K, results=tuple(results))


async def test_hybrid_beats_the_full_text_baseline(
    committing_session: AsyncSession, embedded_corpus: SeededCorpus, embedder: LocalEmbedder
) -> None:
    """The exit criterion, as a number rather than a claim.

    The comparison is against the committed baseline, which was recorded before
    any of this existed. If hybrid does not win, that is a real result and the
    honest response is to keep full text and say why - not to tune until the
    number agrees.
    """
    report = await run_hybrid(committing_session, embedded_corpus, embedder)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    summary = (
        f"\nlexical baseline: nDCG={baseline['ndcg']:.3f} recall={baseline['recall']:.3f}\n"
        f"hybrid          : nDCG={report.ndcg:.3f} recall={report.recall:.3f}\n"
    )
    assert report.ndcg > baseline["ndcg"], f"hybrid did not improve nDCG{summary}"


async def test_hybrid_leaks_no_retired_memories(
    committing_session: AsyncSession, embedded_corpus: SeededCorpus, embedder: LocalEmbedder
) -> None:
    """The assertion a vector index most threatens.

    Similarity has no opinion about what is current, and several queries here are
    built so the *retired* memory is the better semantic match - `q02` asks for
    "redis" when the retired memory is about Redis and the current one mentions
    it only to say it was removed.

    A pure vector store returns the retired memory first on those. This does not,
    because the semantic retriever composes on the same stage-0 filter as
    everything else, so a superseded memory is never a candidate at all.
    """
    report = await run_hybrid(committing_session, embedded_corpus, embedder)

    assert report.stale_inclusion_rate == 0.0, (
        "adding a semantic retriever reintroduced retired memories:\n"
        + "\n".join(
            f"  {r.query.id} {r.query.query!r} -> {sorted(set(r.ranked) & set(r.query.forbidden))}"
            for r in report.leaks
        )
    )


async def test_the_known_vocabulary_gaps_are_closed(
    committing_session: AsyncSession, embedded_corpus: SeededCorpus, embedder: LocalEmbedder
) -> None:
    """The three queries full text could not answer at all.

    Recorded as failures before embeddings existed, precisely so this could be
    checked rather than asserted:

    * ``jwt`` - the stemmer does not reduce "JWTs" to "jwt"
    * ``deadlock prevention`` - the memory describes it without the word
    * ``what is being worked on right now`` - "worked" versus "implementing"

    Each is a vocabulary gap rather than a ranking problem, which is exactly what
    semantic retrieval is for. If they are still zero, hybrid is not doing the
    one thing it was added to do.
    """
    report = await run_hybrid(committing_session, embedded_corpus, embedder)
    by_id = {r.query.id: r for r in report.results}

    improved = {qid: by_id[qid].ndcg for qid in ("q13", "q22", "q31")}
    assert any(score > 0 for score in improved.values()), (
        f"none of the vocabulary-gap queries improved: {improved}. "
        "Semantic retrieval was added specifically for these."
    )
