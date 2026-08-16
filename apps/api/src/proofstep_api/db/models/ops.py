"""Operational records: background jobs that failed for good.

One table, and the reason it exists rather than "read the worker's logs": a job that has
exhausted its retries has *silently stopped doing its work*, and every symptom of that is
absence. Online evaluations stop being written, review queues stop filling, retention stops
running — and all of those look exactly like a quiet week. Logs record it, but nobody greps
logs for a thing they do not know happened.

Deliberately **not** tenant-scoped. These jobs sweep every project, so a failure belongs to
the deployment rather than to a tenant, and giving the row a `project_id` would mean either
inventing one or writing a row per project for a single failure. That is why the table is in
`rls.UNPROTECTED_TABLES` with a stated reason instead of carrying a policy.

Deliberately **without a traceback**. A driver-level exception message can echo the statement
and its bound parameters, and a traceback frame can hold whole request bodies — which for this
system means trace payloads. The full traceback goes to the process log, where the operator
already controls retention and access; what lands in the database is the type and a truncated
message, which is enough to tell one recurring failure from another.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from proofstep_api.db.base import IdentifiedBase, TimestampMixin, jsonb_default

#: Cap on the stored message. Long enough to identify the failure, short enough that a driver
#: error echoing a large statement does not turn this table into an accidental payload store.
MAX_MESSAGE_CHARS = 2_000


class DeadLetterJob(IdentifiedBase, TimestampMixin):
    """A background job that failed every attempt it was given."""

    __tablename__ = "worker_dead_letters"
    __table_args__ = (
        # The two queries an operator actually runs: "what is broken now" (unresolved, newest
        # first) and "is this job failing repeatedly" (by name over a window).
        Index("ix_worker_dead_letters_unresolved", "resolved_at", "created_at"),
        Index("ix_worker_dead_letters_job_name_created_at", "job_name", "created_at"),
    )

    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    #: arq's job id when the failure came from the queue, absent when a job was run by hand.
    #: Not unique: a redeploy can reuse an id, and losing a second failure to a constraint
    #: violation would be the opposite of what this table is for.
    job_id: Mapped[str | None] = mapped_column(String(100), default=None)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    error_type: Mapped[str] = mapped_column(String(200), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)

    #: Job arguments, when they are safe to keep — these jobs take project ids, limits, and
    #: windows, never content.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=jsonb_default, nullable=False)

    #: Set when an operator has dealt with it. Kept rather than deleted, so "this job has
    #: failed on eleven separate days" remains answerable after each one is handled.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolution: Mapped[str | None] = mapped_column(Text, default=None)


class WorkerHeartbeat(IdentifiedBase, TimestampMixin):
    """The last time a worker was known to be alive.

    A row rather than a Redis key with a TTL, for one reason: when Redis is the thing that broke,
    a TTL-based liveness signal disappears at exactly the moment it is needed, and "the worker is
    down" becomes indistinguishable from "I cannot tell". A row in the database the worker was
    already writing to survives that.

    Written on every job and on a dedicated one-minute cron, so a worker that is up but wedged on a
    single long job still reports — the cron and the job queue share a process, so a heartbeat that
    stops means the process stopped, which is the thing being detected.
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        # One row per worker name, updated in place. History would grow without bound to answer a
        # question nobody asks: what matters is the age of the newest beat, not the sequence.
        UniqueConstraint("worker_name", name="uq_worker_heartbeats_worker_name"),
    )

    worker_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: What it last did, for the operator reading this during an incident.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=jsonb_default, nullable=False)


__all__ = ["MAX_MESSAGE_CHARS", "DeadLetterJob", "WorkerHeartbeat"]
