"""Metric implementations, against worked examples.

An evaluation harness whose metrics are wrong is worse than none: it produces
confident numbers that justify the wrong decisions. So every metric here is
checked against a case where the right answer can be computed by hand, and
against the degenerate inputs that a real corpus produces - no relevant results,
an empty ranking, a query nothing answers.
"""

from __future__ import annotations

import math

import pytest

from memhub.evaluation import metrics
from memhub.evaluation.harness import GradedQuery, Report, score_query


class TestDCG:
    def test_worked_example(self) -> None:
        """Computed by hand: gains 2, 0, 1 at positions 1, 2, 3.

        (2^2-1)/log2(2) + (2^0-1)/log2(3) + (2^1-1)/log2(4)
        = 3/1 + 0 + 1/2 = 3.5
        """
        assert metrics.dcg([2, 0, 1]) == pytest.approx(3.5)

    def test_position_matters(self) -> None:
        assert metrics.dcg([2, 1]) > metrics.dcg([1, 2])

    def test_exponential_gain_separates_the_grades(self) -> None:
        """A directly-answering result is worth 3, a merely useful one 1.

        Linear gain would make them 2 and 1, understating how much better a real
        answer is than a related note.
        """
        assert metrics.dcg([2]) == pytest.approx(3.0)
        assert metrics.dcg([1]) == pytest.approx(1.0)

    def test_empty(self) -> None:
        assert metrics.dcg([]) == 0.0


class TestNDCG:
    def test_perfect_ranking_scores_one(self) -> None:
        judgments = {"a": 2, "b": 1}
        assert metrics.ndcg_at_k(["a", "b"], judgments, k=10) == pytest.approx(1.0)

    def test_reversed_ranking_scores_less(self) -> None:
        judgments = {"a": 2, "b": 1}
        assert metrics.ndcg_at_k(["b", "a"], judgments, k=10) < 1.0

    def test_irrelevant_results_do_not_help(self) -> None:
        judgments = {"a": 2}
        assert metrics.ndcg_at_k(["x", "y", "a"], judgments, k=10) < metrics.ndcg_at_k(
            ["a"], judgments, k=10
        )

    def test_cutoff_is_respected(self) -> None:
        judgments = {"a": 2}
        assert metrics.ndcg_at_k(["x", "y", "a"], judgments, k=2) == 0.0

    def test_no_relevant_memories_scores_zero_not_one(self) -> None:
        """A query with nothing to find is not perfectly answered.

        Returning 1.0 here would let unanswerable queries inflate the mean, which
        is why the harness excludes them rather than scoring them.
        """
        assert metrics.ndcg_at_k(["a", "b"], {}, k=10) == 0.0

    def test_empty_ranking(self) -> None:
        assert metrics.ndcg_at_k([], {"a": 2}, k=10) == 0.0

    def test_known_value(self) -> None:
        """One relevant result at position 2, judged 2.

        DCG = (2^2-1)/log2(3) = 3/1.585 = 1.893
        IDCG = 3/1 = 3
        nDCG = 0.631
        """
        assert metrics.ndcg_at_k(["x", "a"], {"a": 2}, k=10) == pytest.approx(
            3 / math.log2(3) / 3, abs=1e-4
        )


class TestRecallAndPrecision:
    def test_recall_counts_any_positive_grade(self) -> None:
        judgments = {"a": 2, "b": 1, "c": 0}
        assert metrics.recall_at_k(["a"], judgments, k=10) == pytest.approx(0.5)
        assert metrics.recall_at_k(["a", "b"], judgments, k=10) == pytest.approx(1.0)

    def test_recall_ignores_zero_graded_entries(self) -> None:
        """A judgment of 0 means irrelevant, so it is not something to find."""
        assert metrics.recall_at_k(["c"], {"a": 2, "c": 0}, k=10) == 0.0

    def test_precision_penalises_noise(self) -> None:
        judgments = {"a": 2}
        assert metrics.precision_at_k(["a"], judgments, k=10) == pytest.approx(1.0)
        assert metrics.precision_at_k(["a", "x", "y", "z"], judgments, k=10) == pytest.approx(0.25)

    def test_precision_of_nothing_is_zero(self) -> None:
        assert metrics.precision_at_k([], {"a": 2}, k=10) == 0.0


class TestReciprocalRank:
    def test_first_position(self) -> None:
        assert metrics.reciprocal_rank(["a"], {"a": 1}) == pytest.approx(1.0)

    def test_third_position(self) -> None:
        assert metrics.reciprocal_rank(["x", "y", "a"], {"a": 1}) == pytest.approx(1 / 3)

    def test_nothing_relevant(self) -> None:
        assert metrics.reciprocal_rank(["x"], {"a": 1}) == 0.0


class TestStaleInclusion:
    def test_detects_a_forbidden_result(self) -> None:
        assert metrics.stale_inclusion(["a", "old"], ["old"], k=10) is True

    def test_clean_result(self) -> None:
        assert metrics.stale_inclusion(["a", "b"], ["old"], k=10) is False

    def test_respects_the_cutoff(self) -> None:
        """Beyond k it is not returned to the caller, so it did not leak."""
        assert metrics.stale_inclusion(["a", "b", "old"], ["old"], k=2) is False


class TestReport:
    def test_means_exclude_unanswerable_queries(self) -> None:
        """Otherwise a corpus with many unanswerable queries scores badly for
        having nothing to find, which says nothing about the retriever."""
        answerable = GradedQuery(id="q1", query="x", relevant={"a": 2})
        unanswerable = GradedQuery(id="q2", query="y", relevant={})

        report = Report(
            k=10,
            results=(
                score_query(answerable, ["a"]),
                score_query(unanswerable, []),
            ),
        )
        assert report.ndcg == pytest.approx(1.0)
        assert len(report.answerable) == 1

    def test_stale_rate_counts_every_query(self) -> None:
        """Including unanswerable ones - the cross-project traps have no relevant
        memories at all, and a leak there is exactly as serious."""
        trap = GradedQuery(id="q1", query="x", relevant={}, forbidden=("foreign",))
        clean = GradedQuery(id="q2", query="y", relevant={"a": 2})

        report = Report(
            k=10,
            results=(
                score_query(trap, ["foreign"]),
                score_query(clean, ["a"]),
            ),
        )
        assert report.stale_inclusion_rate == pytest.approx(0.5)
        assert len(report.leaks) == 1

    def test_empty_for_unanswerable(self) -> None:
        good = GradedQuery(id="q1", query="x", relevant={})
        bad = GradedQuery(id="q2", query="y", relevant={})
        report = Report(
            k=10,
            results=(score_query(good, []), score_query(bad, ["junk"])),
        )
        assert report.empty_for_unanswerable == pytest.approx(0.5)

    def test_empty_report_does_not_divide_by_zero(self) -> None:
        report = Report(k=10)
        assert report.ndcg == 0.0
        assert report.stale_inclusion_rate == 0.0
        assert report.empty_for_unanswerable == 1.0
