"""Metrics.

A deliberately small in-process registry. There is no OpenTelemetry exporter
yet: the server is a short-lived stdio subprocess, so pull-based scraping cannot
find it and push-based OTLP needs a collector to push to. Wiring that up is
Milestone 9's job. What is needed *now* is the ability to count conflicts and
replays while building the concurrency work, and to have the label discipline
fixed before there are call sites to retrofit.

**The rule this module enforces: labels are bounded-cardinality.**

``memory_id``, ``project_id`` and ``request_id`` must never become labels. Each
distinct label combination is a separate time series, so a label carrying a UUID
turns one metric into one series per memory and takes the metrics backend down.
Those identifiers belong in logs and traces, where high cardinality is the point.

Rather than documenting that rule and hoping, :func:`record` rejects a forbidden
label at the call site. The check is cheap and it fails during development
rather than in production.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Final

# Identifiers that must never be used as a metric label. This is the guard
# against the cardinality explosion described in architecture section 11.2.
FORBIDDEN_LABELS: Final[frozenset[str]] = frozenset(
    {"memory_id", "project_id", "request_id", "session_id", "content", "slug", "user_id"}
)

# Client names are caller-supplied: a client may send any string it likes.
# Anything outside this set collapses to "other", so a mistyped or hostile client
# name cannot create an unbounded number of series.
KNOWN_CLIENTS: Final[frozenset[str]] = frozenset({"claude-desktop", "cursor", "unknown"})

Labels = tuple[tuple[str, str], ...]


def client_label(name: str) -> str:
    """Collapse an arbitrary client name onto a bounded set."""
    normalised = name.strip().casefold()
    return normalised if normalised in KNOWN_CLIENTS else "other"


def _freeze(labels: dict[str, str]) -> Labels:
    for key in labels:
        if key in FORBIDDEN_LABELS:
            raise ValueError(
                f"{key!r} must not be used as a metric label: it is unbounded and "
                "would create one time series per value. Put it in a log record "
                "or a trace attribute instead."
            )
    return tuple(sorted((key, str(value)) for key, value in labels.items()))


@dataclass
class Histogram:
    count: int = 0
    total: float = 0.0
    buckets: dict[float, int] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0


# Bucket boundaries in seconds. Chosen around the latency targets in the
# architecture document: search p95 under 50ms, context build p95 under 150ms.
DEFAULT_BUCKETS: Final[tuple[float, ...]] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
)


class MetricsRegistry:
    """Thread-safe counters and histograms.

    Thread-safe rather than merely async-safe because the embedding worker in
    Milestone 7 may run in a thread, and a metrics registry that needs the caller
    to think about which context it is in is a metrics registry that will be got
    wrong.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._histograms: dict[str, dict[Labels, Histogram]] = defaultdict(dict)

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _freeze(labels)
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _freeze(labels)
        with self._lock:
            series = self._histograms[name]
            histogram = series.setdefault(key, Histogram())
            histogram.count += 1
            histogram.total += value
            for boundary in DEFAULT_BUCKETS:
                if value <= boundary:
                    histogram.buckets[boundary] = histogram.buckets.get(boundary, 0) + 1

    def counter(self, name: str, **labels: str) -> float:
        with self._lock:
            return self._counters.get(name, {}).get(_freeze(labels), 0.0)

    def histogram(self, name: str, **labels: str) -> Histogram | None:
        with self._lock:
            return self._histograms.get(name, {}).get(_freeze(labels))

    def series_count(self) -> int:
        """Total distinct time series held.

        Exposed so a test can assert that a realistic workload does not grow
        series without bound - the symptom of a label that should not be one.
        """
        with self._lock:
            return sum(len(s) for s in self._counters.values()) + sum(
                len(s) for s in self._histograms.values()
            )

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


# Metric names, from architecture section 11.2. Declared as constants so a typo
# creates an import error rather than a silently separate metric.
WRITES = "memhub_writes_total"
REVISIONS = "memhub_revisions_total"
CONFLICTS = "memhub_conflicts_total"
SUPERSESSIONS = "memhub_supersessions_total"
DEDUPLICATIONS = "memhub_deduplications_total"
FORGETS = "memhub_forgets_total"
SEARCH_WIDENED = "memhub_search_widened_total"
EMBEDDINGS = "memhub_embeddings_total"
EMBEDDING_FAILURES = "memhub_embedding_failures_total"
EMBEDDING_QUEUE_DEPTH = "memhub_embedding_queue_depth"
SEARCHES = "memhub_searches_total"
IDEMPOTENT_REPLAYS = "memhub_idempotent_replays_total"
TOOL_CALLS = "memhub_tool_calls_total"
TOOL_LATENCY = "memhub_tool_latency_seconds"

_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _registry
