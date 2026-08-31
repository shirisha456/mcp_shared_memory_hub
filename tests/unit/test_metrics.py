"""Metric label discipline.

The only behaviour here worth testing is the cardinality guard. A metric label
carrying a UUID creates one time series per value, which takes a metrics backend
down - and it is an easy mistake to make, because the code reads perfectly well.
Enforcing it at the call site means the mistake fails during development rather
than in production.
"""

from __future__ import annotations

import pytest

from memhub.observability.metrics import (
    FORBIDDEN_LABELS,
    MetricsRegistry,
    client_label,
)


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


class TestCardinalityGuard:
    @pytest.mark.parametrize("label", sorted(FORBIDDEN_LABELS))
    def test_unbounded_labels_are_refused(self, registry: MetricsRegistry, label: str) -> None:
        with pytest.raises(ValueError, match="must not be used as a metric label"):
            registry.increment("memhub_writes_total", 1.0, **{label: "some-value"})

    def test_the_refusal_says_where_it_belongs_instead(self, registry: MetricsRegistry) -> None:
        """An error that only says 'no' invites someone to work around it."""
        with pytest.raises(ValueError, match="log record or a trace attribute"):
            registry.increment("memhub_writes_total", memory_id="abc")

    def test_bounded_labels_are_allowed(self, registry: MetricsRegistry) -> None:
        registry.increment("memhub_writes_total", type="DECISION", outcome="created")
        assert registry.counter("memhub_writes_total", type="DECISION", outcome="created") == 1

    def test_histograms_are_guarded_too(self, registry: MetricsRegistry) -> None:
        with pytest.raises(ValueError):
            registry.observe("memhub_tool_latency_seconds", 0.1, project_id="abc")

    def test_a_realistic_workload_does_not_grow_series_without_bound(
        self, registry: MetricsRegistry
    ) -> None:
        """1000 writes across 4 types and 3 outcomes is at most 12 series."""
        for i in range(1000):
            registry.increment(
                "memhub_writes_total",
                type=["DECISION", "CONSTRAINT", "FACT", "TASK"][i % 4],
                outcome=["created", "idempotent_replay", "deduplicated"][i % 3],
            )
        assert registry.series_count() <= 12


class TestClientLabel:
    @pytest.mark.parametrize("name", ["claude-desktop", "cursor", "unknown"])
    def test_known_clients_pass_through(self, name: str) -> None:
        assert client_label(name) == name

    def test_case_and_whitespace_are_normalised(self) -> None:
        assert client_label("  Claude-Desktop  ") == "claude-desktop"

    def test_unknown_clients_collapse_to_other(self) -> None:
        """Client names are caller-supplied, so an arbitrary string must not
        become an arbitrary time series."""
        assert client_label("some-tool-we-have-never-heard-of") == "other"
        assert client_label("") == "other"

    def test_a_thousand_distinct_client_names_make_one_series(self) -> None:
        registry = MetricsRegistry()
        for i in range(1000):
            registry.increment("memhub_tool_calls_total", client=client_label(f"client-{i}"))
        assert registry.series_count() == 1


class TestAccumulation:
    def test_counters_add_up(self, registry: MetricsRegistry) -> None:
        for _ in range(5):
            registry.increment("memhub_conflicts_total")
        assert registry.counter("memhub_conflicts_total") == 5

    def test_label_order_does_not_create_separate_series(self, registry: MetricsRegistry) -> None:
        """Labels are sorted before use, so kwargs order cannot silently split
        one metric into two."""
        registry.increment("memhub_writes_total", type="FACT", outcome="created")
        registry.increment("memhub_writes_total", outcome="created", type="FACT")
        assert registry.counter("memhub_writes_total", type="FACT", outcome="created") == 2
        assert registry.series_count() == 1

    def test_histogram_records_count_and_total(self, registry: MetricsRegistry) -> None:
        for value in (0.01, 0.02, 0.03):
            registry.observe("memhub_tool_latency_seconds", value, tool="memory_search")
        histogram = registry.histogram("memhub_tool_latency_seconds", tool="memory_search")
        assert histogram is not None
        assert histogram.count == 3
        assert histogram.mean == pytest.approx(0.02)

    def test_unrecorded_counter_reads_zero(self, registry: MetricsRegistry) -> None:
        assert registry.counter("memhub_writes_total", type="FACT", outcome="created") == 0
