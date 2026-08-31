"""Loading and seeding the evaluation corpus.

The loader lives in tests rather than in the package because it needs PyYAML,
which is a development dependency: the running server never reads this dataset.
The metrics and harness stay in ``memhub.evaluation`` so they are type-checked
and unit-tested like everything else.

Distractors pad the hand-written core to a realistic corpus size. They exist so
that queries face genuine competition - a 30-memory corpus where every query has
one plausible answer measures very little - but nothing judges them, and any
distractor appearing in a top-10 result simply costs precision, which is the
correct penalty.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from memhub.domain.enums import MemoryType
from memhub.evaluation.harness import GradedQuery
from memhub.services.memories import remember
from memhub.services.projects import use_project

DATASET_DIR = Path(__file__).resolve().parents[2] / "eval" / "dataset"
BASELINE = DATASET_DIR / "baseline.json"
HISTORY = DATASET_DIR / "history.json"
RESULTS = Path(__file__).resolve().parents[2] / "docs" / "eval" / "results.md"

TOTAL_CORPUS_SIZE = 200
DISTRACTOR_SEED = 20260601


@dataclass(frozen=True, slots=True)
class SeededCorpus:
    project_id: uuid.UUID
    other_project_id: uuid.UUID
    by_eval_id: dict[str, uuid.UUID]
    """Maps the dataset's stable ids (m102, x101) to real memory ids."""

    to_eval_id: dict[uuid.UUID, str]
    size: int


def load_memories() -> dict[str, Any]:
    return dict(yaml.safe_load((DATASET_DIR / "memories.yaml").read_text(encoding="utf-8")))


def load_queries() -> list[GradedQuery]:
    raw = yaml.safe_load((DATASET_DIR / "queries.yaml").read_text(encoding="utf-8"))
    return [
        GradedQuery(
            id=item["id"],
            query=item["query"],
            relevant=dict(item.get("relevant") or {}),
            forbidden=tuple(item.get("forbidden") or ()),
            note=item.get("note"),
        )
        for item in raw["queries"]
    ]


def distractor_contents(count: int, *, seed: int = DISTRACTOR_SEED) -> list[str]:
    """Plausible but unjudged memories, generated deterministically."""
    rng = random.Random(seed)
    subjects = [
        "The report generator",
        "The email digest",
        "The CSV importer",
        "The admin console",
        "The onboarding flow",
        "The archive job",
        "The image thumbnailer",
        "The changelog builder",
        "The locale loader",
        "The permissions cache",
        "The invoice renderer",
        "The sitemap task",
    ]
    predicates = [
        "reads its configuration from environment variables at startup",
        "is disabled by default in development",
        "writes progress to a temporary table",
        "runs on a fixed schedule rather than on demand",
        "was extracted from the monolith in the second quarter",
        "has no automated tests beyond a smoke check",
        "buffers output before flushing to reduce syscalls",
        "skips records older than ninety days",
        "reports failures to the incident channel",
        "shares a template renderer with the notification service",
    ]
    return [f"{rng.choice(subjects)} {rng.choice(predicates)} (detail {i})." for i in range(count)]


async def seed_corpus(session: AsyncSession) -> SeededCorpus:
    """Write the corpus, applying supersession exactly as a client would.

    Superseded memories are created first, then retired by the memory that
    replaces them - through the real ``remember(supersedes=...)`` path, not by
    writing a status directly. Otherwise the evaluation would be measuring a
    state the system cannot actually produce.
    """
    data = load_memories()
    project = await use_project(session, slug=data["project"], create=True)
    other = await use_project(session, slug=data["other_project"], create=True)

    by_eval_id: dict[str, uuid.UUID] = {}
    entries = list(data["memories"])

    # Anything that supersedes something else must be written after its target.
    superseding = {e["superseded_by"] for e in entries if e.get("superseded_by")}
    ordered = [e for e in entries if e["id"] not in superseding]
    ordered += [e for e in entries if e["id"] in superseding]

    retires: dict[str, list[str]] = {}
    for entry in entries:
        if target := entry.get("superseded_by"):
            retires.setdefault(target, []).append(entry["id"])

    for entry in ordered:
        expires_at = None
        if days := entry.get("expires_in_days"):
            expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=int(days))

        result = await remember(
            session,
            project.id,
            memory_type=MemoryType(entry["type"]),
            content=" ".join(entry["content"].split()),
            tags=entry.get("tags"),
            importance=entry.get("importance"),
            expires_at=expires_at,
            supersedes=[by_eval_id[old] for old in retires.get(entry["id"], [])] or None,
            author_client="claude-desktop",
        )
        by_eval_id[entry["id"]] = result.memory.memory_id

    for entry in data["other_memories"]:
        result = await remember(
            session,
            other.id,
            memory_type=MemoryType(entry["type"]),
            content=" ".join(entry["content"].split()),
            tags=entry.get("tags"),
            importance=entry.get("importance"),
            author_client="cursor",
        )
        by_eval_id[entry["id"]] = result.memory.memory_id

    padding = TOTAL_CORPUS_SIZE - len(entries)
    for index, content in enumerate(distractor_contents(padding)):
        result = await remember(
            session,
            project.id,
            memory_type=MemoryType.FACT,
            content=content,
            importance=30,
            author_client="cursor",
        )
        by_eval_id[f"d{index:03d}"] = result.memory.memory_id

    return SeededCorpus(
        project_id=project.id,
        other_project_id=other.id,
        by_eval_id=by_eval_id,
        to_eval_id={memory_id: name for name, memory_id in by_eval_id.items()},
        size=len(entries) + padding,
    )
