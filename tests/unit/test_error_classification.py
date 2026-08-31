"""Driver failures become advice the caller can act on.

Architecture section 9 promises three distinct outcomes for infrastructure
failure, and the distinction between them is not cosmetic: two are safe to retry
and one is not. A single opaque error would collapse that distinction and
either strand a caller who could have retried, or invite a duplicate write from
one who should not have.
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from memhub.domain.errors import (
    BackendBusyError,
    BackendUnavailableError,
    DeadlineExceededError,
    MemhubError,
    UnknownOutcomeError,
)
from memhub.mcp.mapping import classify_infrastructure_error, domain_errors


class FakeAsyncpgError(Exception):
    """Stands in for an asyncpg error carrying a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"pgcode {sqlstate}")
        self.sqlstate = sqlstate


def dbapi(orig: Exception, *, invalidated: bool = False) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, orig, connection_invalidated=invalidated)


class TestSafeToRetry:
    def test_pool_exhaustion_is_busy_not_broken(self) -> None:
        classified = classify_infrastructure_error(PoolTimeoutError("pool timed out"))
        assert isinstance(classified, BackendBusyError)
        assert classified.code == "BACKEND_BUSY"
        assert classified.details["retryable"] is True

    def test_a_refused_connection_is_unavailable(self) -> None:
        """Raised unwrapped: there is no cursor for SQLAlchemy to attach it to."""
        classified = classify_infrastructure_error(ConnectionRefusedError(61, "refused"))
        assert isinstance(classified, BackendUnavailableError)
        assert classified.details["retryable"] is True

    def test_an_unresolvable_host_is_unavailable(self) -> None:
        classified = classify_infrastructure_error(socket.gaierror("no such host"))
        assert isinstance(classified, BackendUnavailableError)

    @pytest.mark.parametrize("pgcode", ["08006", "08003", "08000", "57P01"])
    def test_connection_sqlstates_are_unavailable(self, pgcode: str) -> None:
        """57P01 is an administrative shutdown - a restart, not a bug."""
        classified = classify_infrastructure_error(dbapi(FakeAsyncpgError(pgcode)))
        assert isinstance(classified, BackendUnavailableError)

    def test_the_message_says_nothing_was_written(self) -> None:
        """The claim that makes retrying safe has to be stated, not implied."""
        classified = classify_infrastructure_error(ConnectionRefusedError(61, "refused"))
        assert classified is not None
        assert "nothing was written" in classified.message.lower()


class TestNotSafeToRetry:
    def test_a_lost_connection_mid_flight_is_an_unknown_outcome(self) -> None:
        """The one genuinely ambiguous failure.

        ``connection_invalidated`` means the connection had been established and
        then died, so a statement may have run. Whether it committed is exactly
        the fact the lost acknowledgement would have carried.
        """
        classified = classify_infrastructure_error(
            dbapi(FakeAsyncpgError("08006"), invalidated=True)
        )
        assert isinstance(classified, UnknownOutcomeError)
        assert classified.code == "UNKNOWN_OUTCOME"

    def test_invalidation_outranks_the_sqlstate(self) -> None:
        """Both signals are present in a real mid-transaction drop.

        The same 08006 appears whether the connection died before or during a
        statement. Only ``connection_invalidated`` separates them, so it has to
        be checked first - the opposite order would report every mid-flight
        disconnect as safe to retry, which is how duplicate writes happen.
        """
        established = classify_infrastructure_error(
            dbapi(FakeAsyncpgError("08006"), invalidated=True)
        )
        never_opened = classify_infrastructure_error(dbapi(FakeAsyncpgError("08006")))
        assert isinstance(established, UnknownOutcomeError)
        assert isinstance(never_opened, BackendUnavailableError)

    def test_it_names_both_ways_out(self) -> None:
        classified = classify_infrastructure_error(
            dbapi(FakeAsyncpgError("08006"), invalidated=True)
        )
        assert classified is not None
        assert "idempotency key" in classified.message
        assert "re-read" in classified.message
        assert classified.details["retryable"] is False

    def test_a_cancelled_statement_is_a_deadline_not_an_outage(self) -> None:
        """57014 means the query was too slow, not that the database is down.

        Reporting it as unavailable would send the caller into a retry loop
        against a perfectly healthy server, re-running the same slow query.
        """
        classified = classify_infrastructure_error(dbapi(FakeAsyncpgError("57014")))
        assert isinstance(classified, DeadlineExceededError)
        assert classified.details["retryable"] is False


class TestUnrecognised:
    def test_an_unfamiliar_error_is_not_classified(self) -> None:
        """Guessing would produce confident, possibly wrong, retry advice."""
        assert classify_infrastructure_error(ValueError("something else")) is None

    def test_a_plain_sql_error_is_not_an_infrastructure_failure(self) -> None:
        """23505 is a constraint violation - a bug or a conflict, not an outage."""
        assert classify_infrastructure_error(dbapi(FakeAsyncpgError("23505"))) is None

    def test_a_domain_error_is_left_alone(self) -> None:
        """Domain errors take the other branch of the boundary entirely."""
        assert classify_infrastructure_error(MemhubError("nope")) is None


class TestEveryOutcomeIsDistinct:
    def test_the_four_codes_do_not_collide(self) -> None:
        """A client branching on these codes needs them to mean different things."""
        codes = {
            BackendBusyError.code,
            BackendUnavailableError.code,
            UnknownOutcomeError.code,
            DeadlineExceededError.code,
        }
        assert len(codes) == 4


class TestTheBoundaryActuallyUsesIt:
    """Classification is worthless if the decorator does not reach for it."""

    async def test_a_driver_failure_reaches_the_model_with_its_code(self) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        @domain_errors
        async def tool(project: str) -> str:
            raise dbapi(FakeAsyncpgError("08006"))

        with pytest.raises(ToolError) as raised:
            await tool("demo")
        assert "[BACKEND_UNAVAILABLE]" in str(raised.value)

    async def test_an_unrecognised_exception_still_propagates_as_a_bug(self) -> None:
        """Swallowing these would turn every bug into a plausible-looking outage."""

        @domain_errors
        async def tool(project: str) -> str:
            raise ValueError("a real bug")

        with pytest.raises(ValueError):
            await tool("demo")

    async def test_the_signature_survives_classification(self) -> None:
        """The same ``functools.wraps`` constraint as the domain path.

        The SDK builds each tool's input schema from the handler signature. A
        wrapper that lost it would advertise a parameterless tool, which fails
        at the protocol layer rather than here.
        """
        import inspect

        @domain_errors
        async def tool(project: str, limit: int = 10) -> str:
            return project

        assert list(inspect.signature(tool).parameters) == ["project", "limit"]
