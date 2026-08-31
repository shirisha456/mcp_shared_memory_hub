"""Logging tests.

The stdout assertions here are not style checks. Under the stdio transport,
stdout carries the MCP JSON-RPC frames; anything else written there corrupts the
stream and the client fails with an opaque parse error. This is the cheapest
possible guard against the single most common way an MCP server breaks.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from memhub.observability.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def test_logging_never_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="DEBUG", log_format="json")

    log = get_logger("memhub.test")
    log.debug("debug line")
    log.info("info line")
    log.warning("warning line")
    log.error("error line")

    captured = capsys.readouterr()
    assert captured.out == "", (
        "Something wrote to stdout. Under stdio transport this corrupts the "
        f"JSON-RPC stream. Offending output: {captured.out!r}"
    )
    assert "info line" in captured.err


def test_configure_logging_refuses_stdout_explicitly() -> None:
    with pytest.raises(ValueError, match="Refusing to log to stdout"):
        configure_logging(stream=sys.stdout)


def test_json_format_is_one_valid_object_per_line() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", log_format="json", stream=stream)

    get_logger("memhub.test").info("wrote memory", extra={"memory_id": "abc", "revision_no": 3})

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1

    payload = json.loads(lines[0])
    assert payload["level"] == "INFO"
    assert payload["logger"] == "memhub.test"
    assert payload["message"] == "wrote memory"
    assert payload["service"] == "memhub"
    assert payload["memory_id"] == "abc"
    assert payload["revision_no"] == 3
    assert payload["ts"].endswith("+00:00")


def test_exceptions_are_captured_as_a_field() -> None:
    stream = io.StringIO()
    configure_logging(log_format="json", stream=stream)

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("memhub.test").exception("failed")

    payload = json.loads(stream.getvalue().splitlines()[0])
    assert "RuntimeError: boom" in payload["exception"]


def test_configure_logging_is_idempotent() -> None:
    """Repeated configuration must replace the handler, not stack them.

    Stacked handlers duplicate every line, which in a JSON log stream looks like
    duplicated events rather than a logging bug.
    """
    stream = io.StringIO()
    configure_logging(log_format="json", stream=stream)
    configure_logging(log_format="json", stream=stream)
    configure_logging(log_format="json", stream=stream)

    get_logger("memhub.test").info("once")

    assert len([line for line in stream.getvalue().splitlines() if line]) == 1


def test_text_format_renders_extras() -> None:
    stream = io.StringIO()
    configure_logging(log_format="text", stream=stream)

    get_logger("memhub.test").info("hello", extra={"project_id": "p1"})

    output = stream.getvalue()
    assert "hello" in output
    assert "project_id=p1" in output


def test_level_filtering_applies() -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", log_format="json", stream=stream)

    log = get_logger("memhub.test")
    log.info("suppressed")
    log.warning("kept")

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "kept"
