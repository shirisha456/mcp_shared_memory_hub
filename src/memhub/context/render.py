"""Turning a selection into something a model can read.

The brief is grouped by type rather than listed by score, because that is how it
will be used: an agent about to change something wants to know the constraints
first, then what was decided and why. A flat relevance-ordered list makes the
reader do that sorting themselves, every time.

Provenance is included per item. "Two clients independently recorded this" and
"an agent noticed this once" are different claims, and a brief that flattens them
invites the reader to treat them alike.
"""

from __future__ import annotations

from collections.abc import Sequence

from memhub.context.builder import Candidate
from memhub.domain.enums import AuthorKind, MemoryType

HEADINGS: dict[MemoryType, str] = {
    MemoryType.CONSTRAINT: "Constraints - these must not be violated",
    MemoryType.DECISION: "Decisions - what was chosen, and what it rules out",
    MemoryType.FACT: "Facts",
    MemoryType.TASK: "Currently in progress",
}


def render_brief(selected: Sequence[Candidate], *, project_slug: str) -> str:
    """A grouped markdown brief.

    Returns an explicit statement rather than an empty string when nothing was
    selected. A blank response reads as a failure; "this project has no recorded
    memories yet" is a fact the agent can act on.
    """
    if not selected:
        return (
            f"No memories recorded for {project_slug} yet.\n\n"
            "This is not an error - the project simply has nothing stored. "
            "Record decisions and constraints as they are established."
        )

    lines = [f"# Project memory: {project_slug}", ""]

    for memory_type, heading in HEADINGS.items():
        group = [c for c in selected if c.memory.type is memory_type]
        if not group:
            continue

        lines += [f"## {heading}", ""]
        for candidate in group:
            memory = candidate.memory
            lines.append(f"- {memory.content}")

            marks = []
            if memory.author_kind is AuthorKind.HUMAN_CONFIRMED:
                marks.append("confirmed by the user")
            if memory.source:
                marks.append(f"from {memory.source}")
            marks.append(f"recorded by {memory.author_client}")
            lines.append(f"  _{'; '.join(marks)}_")
        lines.append("")

    lines += [
        "---",
        "",
        "Superseded, deleted and expired memories are excluded. What is above is "
        "what the project currently holds to be true; use memory_history to see "
        "what a memory replaced or why it changed.",
    ]
    return "\n".join(lines)
