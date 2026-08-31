"""Rank fusion.

Pure, so it can be checked against arithmetic done by hand. The properties below
are the reasons RRF was chosen over a weighted score sum, and each one is the
thing that breaks if someone later "simplifies" it back to adding scores.
"""

from __future__ import annotations

import uuid

import pytest

from memhub.retrieval.fusion import RRF_K, reciprocal_rank_fusion

A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
C = uuid.UUID("00000000-0000-4000-8000-00000000000c")


class TestScoring:
    def test_worked_example(self) -> None:
        """First place in one ranking scores 1/(60+1)."""
        fused = reciprocal_rank_fusion({"lexical": [A]})
        assert fused[0].score == pytest.approx(1 / (RRF_K + 1))

    def test_agreement_beats_a_single_first_place(self) -> None:
        """The central property.

        A document ranked second by both retrievers outranks one ranked first by
        only one of them. That is the entire point of fusing: corroboration
        across independent views is stronger evidence than one view's confidence.
        """
        fused = reciprocal_rank_fusion({"lexical": [A, B], "semantic": [C, B]})
        assert fused[0].memory_id == B
        assert fused[0].found_by_both

    def test_earlier_positions_score_higher(self) -> None:
        fused = reciprocal_rank_fusion({"lexical": [A, B, C]})
        assert [r.memory_id for r in fused] == [A, B, C]
        assert fused[0].score > fused[1].score > fused[2].score

    def test_k_damps_the_gap_between_top_positions(self) -> None:
        """Without k, rank 1 would be worth twice rank 2.

        That overweights a single retriever's confident mistake. With k=60 the
        ratio is 61/62, so the top few positions are treated as near-equals and
        agreement decides between them.
        """
        fused = reciprocal_rank_fusion({"lexical": [A, B]})
        ratio = fused[0].score / fused[1].score
        assert 1.0 < ratio < 1.05


class TestRobustness:
    def test_an_empty_retriever_contributes_nothing(self) -> None:
        """The case that matters operationally.

        If the outbox is behind or the embedder is down, the semantic ranking is
        empty. Fusion must degrade to pure lexical order with no special case, no
        zero-filling, and no skew - because that is the difference between a
        graceful degradation and an outage.
        """
        lexical_only = reciprocal_rank_fusion({"lexical": [A, B, C], "semantic": []})
        assert [r.memory_id for r in lexical_only] == [A, B, C]

        alone = reciprocal_rank_fusion({"lexical": [A, B, C]})
        assert [r.score for r in lexical_only] == [r.score for r in alone]

    def test_documents_in_only_one_ranking_still_appear(self) -> None:
        fused = reciprocal_rank_fusion({"lexical": [A], "semantic": [B]})
        assert {r.memory_id for r in fused} == {A, B}
        assert not any(r.found_by_both for r in fused)

    def test_no_rankings_at_all(self) -> None:
        assert reciprocal_rank_fusion({}) == []

    def test_provenance_is_reported(self) -> None:
        """Which retriever found what is needed to explain a result."""
        fused = {r.memory_id: r for r in reciprocal_rank_fusion({"lexical": [A], "semantic": [B]})}
        assert fused[A].lexical_rank == 1
        assert fused[A].semantic_rank is None
        assert fused[B].semantic_rank == 1


class TestDeterminism:
    def test_identical_input_gives_identical_output(self) -> None:
        rankings = {"lexical": [A, B, C], "semantic": [C, A]}
        assert reciprocal_rank_fusion(rankings) == reciprocal_rank_fusion(rankings)

    def test_ties_break_on_id_not_insertion_order(self) -> None:
        """Two documents with equal fused scores must have a stable order.

        Without an explicit tiebreak they would come back in dict order, which is
        stable within a process and not across them - so results would differ
        between the two server processes for no visible reason.
        """
        forward = reciprocal_rank_fusion({"lexical": [A], "semantic": [B]})
        backward = reciprocal_rank_fusion({"lexical": [B], "semantic": [A]})
        assert forward[0].score == pytest.approx(backward[0].score)
        assert [r.memory_id for r in forward] == [r.memory_id for r in backward]


class TestWeights:
    def test_weighting_shifts_the_balance(self) -> None:
        equal = reciprocal_rank_fusion({"lexical": [A], "semantic": [B]})
        assert equal[0].score == pytest.approx(equal[1].score)

        weighted = reciprocal_rank_fusion(
            {"lexical": [A], "semantic": [B]}, weights={"semantic": 2.0}
        )
        assert weighted[0].memory_id == B

    def test_unweighted_retrievers_default_to_one(self) -> None:
        """Weighting before measuring is guesswork; the default is neutral."""
        fused = reciprocal_rank_fusion({"lexical": [A]}, weights={"semantic": 5.0})
        assert fused[0].score == pytest.approx(1 / (RRF_K + 1))
