"""Running the evaluation and summarising it.

Kept separate from the metric functions so the metrics stay pure and testable
against worked examples, and separate from the dataset loader so this can run
against any corpus.

The output is a :class:`Report`, which is deliberately more than a single score.
A retrieval change that raises mean nDCG by 0.03 while surfacing one retired
memory is a regression, and a summary that produced one number could not say so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict

from memhub.evaluation import metrics


class StrategyRecord(TypedDict):
    """One row of the progression table in ``eval/dataset/history.json``.

    Typed rather than left as a loose mapping so a renamed metric key is a
    type error rather than a KeyError while writing the report.
    """

    strategy: str
    ndcg: float
    recall: float
    precision: float
    stale_inclusion_rate: float
    note: NotRequired[str]


@dataclass(frozen=True, slots=True)
class GradedQuery:
    id: str
    query: str
    relevant: Mapping[str, int]
    forbidden: tuple[str, ...] = ()
    note: str | None = None

    @property
    def is_answerable(self) -> bool:
        """Whether any memory in the corpus should be returned.

        Unanswerable queries are excluded from the nDCG and recall means - a
        query with nothing to find cannot be ranked well or badly - but they are
        still checked for forbidden results and reported separately, because
        returning confident nonsense is its own failure.
        """
        return any(grade > 0 for grade in self.relevant.values())


@dataclass(frozen=True, slots=True)
class QueryResult:
    query: GradedQuery
    ranked: tuple[str, ...]
    ndcg: float
    recall: float
    precision: float
    reciprocal_rank: float
    leaked_stale: bool


@dataclass(frozen=True, slots=True)
class Report:
    k: int
    results: tuple[QueryResult, ...] = field(default_factory=tuple)

    @property
    def answerable(self) -> tuple[QueryResult, ...]:
        return tuple(r for r in self.results if r.query.is_answerable)

    def _mean(self, select: Callable[[QueryResult], float]) -> float:
        """Mean over answerable queries.

        Takes an accessor rather than an attribute name: ``getattr`` would make a
        renamed field an AttributeError at report time instead of a type error at
        check time, which is exactly the wrong moment to find out.
        """
        scored = self.answerable
        if not scored:
            return 0.0
        return sum(select(result) for result in scored) / len(scored)

    @property
    def ndcg(self) -> float:
        return self._mean(lambda r: r.ndcg)

    @property
    def recall(self) -> float:
        return self._mean(lambda r: r.recall)

    @property
    def precision(self) -> float:
        return self._mean(lambda r: r.precision)

    @property
    def mrr(self) -> float:
        return self._mean(lambda r: r.reciprocal_rank)

    @property
    def stale_inclusion_rate(self) -> float:
        """Fraction of **all** queries where a forbidden memory appeared.

        Computed over every query, not just answerable ones, because the
        cross-project isolation traps have no relevant memories at all - and a
        leak there is exactly as serious.

        Target: 0.0. This is a correctness metric, not a quality one, and it is
        never traded against nDCG.
        """
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.leaked_stale) / len(self.results)

    @property
    def leaks(self) -> tuple[QueryResult, ...]:
        return tuple(r for r in self.results if r.leaked_stale)

    @property
    def empty_for_unanswerable(self) -> float:
        """Fraction of unanswerable queries that correctly returned nothing."""
        unanswerable = [r for r in self.results if not r.query.is_answerable]
        if not unanswerable:
            return 1.0
        return sum(1 for r in unanswerable if not r.ranked) / len(unanswerable)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "k": self.k,
            "queries": len(self.results),
            "answerable": len(self.answerable),
            "ndcg": round(self.ndcg, 4),
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "mrr": round(self.mrr, 4),
            "stale_inclusion_rate": round(self.stale_inclusion_rate, 4),
            "empty_for_unanswerable": round(self.empty_for_unanswerable, 4),
        }


def score_query(query: GradedQuery, ranked: Sequence[str], *, k: int = 10) -> QueryResult:
    return QueryResult(
        query=query,
        ranked=tuple(ranked),
        ndcg=metrics.ndcg_at_k(ranked, query.relevant, k),
        recall=metrics.recall_at_k(ranked, query.relevant, k),
        precision=metrics.precision_at_k(ranked, query.relevant, k),
        reciprocal_rank=metrics.reciprocal_rank(ranked, query.relevant),
        leaked_stale=metrics.stale_inclusion(ranked, query.forbidden, k),
    )


def render_markdown(
    report: Report,
    *,
    strategy: str,
    corpus_size: int,
    history: Sequence[StrategyRecord] = (),
) -> str:
    """A results table that says what it measured, not just the numbers."""
    lines = [
        "# Retrieval evaluation",
        "",
        f"Strategy: **{strategy}**  ",
        f"Corpus: {corpus_size} memories  ",
        f"Queries: {len(report.results)} ({len(report.answerable)} answerable)  ",
        f"Cutoff: k={report.k}",
        "",
        "| Metric | Value | Target |",
        "|---|---|---|",
        f"| nDCG@{report.k} | {report.ndcg:.3f} | higher is better |",
        f"| Recall@{report.k} | {report.recall:.3f} | higher is better |",
        f"| Precision@{report.k} | {report.precision:.3f} | higher is better |",
        f"| MRR | {report.mrr:.3f} | secondary |",
        f"| **Stale inclusion rate** | **{report.stale_inclusion_rate:.3f}** | **exactly 0** |",
        f"| Empty for unanswerable | {report.empty_for_unanswerable:.3f} | 1.0 |",
        "",
        "Means are over answerable queries only. A query with nothing to find "
        "cannot be ranked well or badly, so including it would just dilute the "
        "score with zeros.",
        "",
        "Stale inclusion is computed over *all* queries, including the "
        "cross-project traps, and is a correctness metric rather than a quality "
        "one - it is never traded against nDCG.",
        "",
    ]

    if report.leaks:
        lines += ["## Leaks", "", "Queries that returned a memory they must not:", ""]
        lines += [
            f"- `{r.query.id}` {r.query.query!r} returned {set(r.ranked) & set(r.query.forbidden)}"
            for r in report.leaks
        ]
        lines.append("")

    if history:
        lines += [
            "## How this got here",
            "",
            "Each row is a retrieval strategy, measured against the same corpus and",
            "the same judgments. The judgments were written before any of them ran.",
            "",
            "| Strategy | nDCG@10 | Recall@10 | Precision@10 | Stale |",
            "|---|---|---|---|---|",
        ]
        for entry in history:
            lines.append(
                f"| {entry['strategy']} | {entry['ndcg']:.3f} | "
                f"{entry['recall']:.3f} | {entry['precision']:.3f} | "
                f"{entry['stale_inclusion_rate']:.3f} |"
            )
        lines.append("")
        for entry in history:
            if note := entry.get("note"):
                lines += [f"**{entry['strategy']}** - {note}", ""]

    worst = sorted(report.answerable, key=lambda r: r.ndcg)[:5]
    lines += [
        "## Weakest queries",
        "",
        "Where the current strategy does worst. These are the cases a later "
        "retriever has to improve, and the reason the baseline is recorded "
        "before that retriever is built.",
        "",
        "| Query | nDCG | Recall |",
        "|---|---|---|",
    ]
    lines += [
        f"| `{r.query.id}` {r.query.query!r} | {r.ndcg:.3f} | {r.recall:.3f} |" for r in worst
    ]
    lines.append("")
    return "\n".join(lines)
