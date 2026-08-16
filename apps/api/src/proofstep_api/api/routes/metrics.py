"""Prometheus exposition for the things that fail quietly.

Deliberately a small, hand-written exporter rather than `prometheus_client`. Not on principle —
that library is good — but because everything here is a **gauge read from the database at scrape
time**, and none of it is a process-local counter that needs a registry, multiprocess collection, or
a metrics middleware. A dependency that exists to solve a problem this endpoint does not have is a
dependency that has to be kept working anyway.

What is exported, and why each one:

- `proofstep_worker_heartbeat_age_seconds` — the alert that matters most. When the worker stops,
  the API keeps returning 200 to everything and the only symptom is that new data stops appearing.
- `proofstep_dead_letters_unresolved` and `_oldest_age_seconds` — jobs that exhausted their retries.
  A count answers "is something broken"; the age answers "is anyone dealing with it", and those are
  different questions with different responses.
- `proofstep_job_queue_depth` / `_scheduled` — a backlog, separated from work that is merely
  deferred, because a healthy cron schedule otherwise reads as a queue nobody is draining.
- `proofstep_review_queue_pending` / `_oldest_age_seconds` — same shape, for the human queue. A
  review queue nobody reads stops being a control while still looking like one.
- `proofstep_rls_enforced` — 1 when row-level security applies to the connected role. This is a
  configuration fact rather than a rate, and it is exported because it is the one security property
  that is invisible from the outside: everything works identically when it is off.
- `proofstep_up`, `proofstep_build_info` — the scrape target itself, so "no data" can be told apart
  from "zero".

**Authentication is required.** Prometheus supports `bearer_token_file`, so a scrape credential is
one line of scrape config; adding an unauthenticated endpoint would be a new public surface on a
service whose entire threat model is about who may read what. The numbers here are counts and ages
with no tenant identifiers, so any project-scoped read key may scrape them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, Response

from proofstep_api.api.dependencies import SessionDep, SettingsDep
from proofstep_api.api.routes.online import Reader
from proofstep_api.services import budget
from proofstep_api.worker import deadletter, jobs

router = APIRouter(tags=["operations"])

#: Prometheus' text exposition format. `version=0.0.4` is what scrapers expect; omitting it makes
#: some clients fall back to a content-type guess.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

VERSION = "0.1.0"


def _line(name: str, value: float | int | None, labels: dict[str, str] | None = None) -> list[str]:
    """One sample, or nothing at all when the value is unknown.

    Omitting an unknown value rather than exporting 0 is the whole discipline of this file. A queue
    depth of 0 means "empty"; a queue that could not be read means "I have no idea", and a dashboard
    that renders the second as the first is how an outage gets watched over for an hour.
    """
    if value is None:
        return []
    rendered = ""
    if labels:
        inner = ",".join(f'{key}="{_escape(val)}"' for key, val in sorted(labels.items()))
        rendered = f"{{{inner}}}"
    return [f"{name}{rendered} {value}"]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _age_seconds(moment: datetime | str | None) -> float | None:
    if moment is None:
        return None
    when = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds())


# In the schema on purpose, despite being a machine endpoint. `test_cross_tenant.py` enumerates the
# OpenAPI schema to prove every route is either swept or explicitly excused, so a route hidden from
# the schema is a route that escapes the sweep — a tidier /docs is not worth that.
@router.get("/metrics", summary="Prometheus metrics")
async def metrics(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    principal: Reader,
) -> Response:
    lines: list[str] = [
        "# HELP proofstep_up The API answered this scrape.",
        "# TYPE proofstep_up gauge",
        "proofstep_up 1",
        "# HELP proofstep_build_info Version, as a constant-1 gauge with labels.",
        "# TYPE proofstep_build_info gauge",
        *_line("proofstep_build_info", 1, {"version": VERSION, "env": settings.env}),
    ]

    lines += [
        "# HELP proofstep_worker_heartbeat_age_seconds Seconds since a worker last reported in.",
        "# TYPE proofstep_worker_heartbeat_age_seconds gauge",
    ]
    beats = await deadletter.heartbeats(session)
    for row in beats:
        lines += _line(
            "proofstep_worker_heartbeat_age_seconds",
            _age_seconds(row.last_seen_at),
            {"worker": row.worker_name},
        )
    # No sample at all when no worker has ever beaten. An age of 0 would read as "just seen", which
    # is the opposite of the truth, and `absent()` is exactly the alert this case needs.
    lines += [
        "# HELP proofstep_workers_known Workers that have reported at least once.",
        "# TYPE proofstep_workers_known gauge",
        *_line("proofstep_workers_known", len(beats)),
    ]

    summary = await deadletter.summary(session, window_hours=24)
    lines += [
        "# HELP proofstep_dead_letters_unresolved Background jobs that exhausted their retries.",
        "# TYPE proofstep_dead_letters_unresolved gauge",
        *_line("proofstep_dead_letters_unresolved", summary["unresolved"]),
        "# HELP proofstep_dead_letters_oldest_age_seconds Age of the oldest unresolved failure.",
        "# TYPE proofstep_dead_letters_oldest_age_seconds gauge",
        *_line(
            "proofstep_dead_letters_oldest_age_seconds",
            _age_seconds(summary["oldest_unresolved"]),
        ),
        "# HELP proofstep_dead_letters_24h Failures recorded in the last 24 hours, by job.",
        "# TYPE proofstep_dead_letters_24h gauge",
    ]
    for job_name, stats in summary["by_job"].items():
        lines += _line("proofstep_dead_letters_24h", stats["failures"], {"job": job_name})

    snapshot = await deadletter.queue_snapshot(settings.redis_url)
    lines += [
        "# HELP proofstep_job_queue_depth Jobs waiting in the queue, ready and deferred together.",
        "# TYPE proofstep_job_queue_depth gauge",
        *_line("proofstep_job_queue_depth", snapshot.depth),
        "# HELP proofstep_job_queue_scheduled Queued jobs whose run time is still in the future.",
        "# TYPE proofstep_job_queue_scheduled gauge",
        *_line("proofstep_job_queue_scheduled", snapshot.scheduled),
        "# HELP proofstep_job_queue_reachable 1 when the queue could be read at all.",
        "# TYPE proofstep_job_queue_reachable gauge",
        *_line("proofstep_job_queue_reachable", 0 if snapshot.error else 1),
    ]

    review = await jobs.queue_health(session, project_id=principal.project)
    lines += [
        "# HELP proofstep_review_queue_pending Items awaiting a human verdict.",
        "# TYPE proofstep_review_queue_pending gauge",
    ]
    for slug, stats in review.items():
        lines += _line("proofstep_review_queue_pending", stats.get("pending", 0), {"queue": slug})
    lines += [
        "# HELP proofstep_review_queue_oldest_age_seconds Age of the oldest pending review item.",
        "# TYPE proofstep_review_queue_oldest_age_seconds gauge",
    ]
    for slug, stats in review.items():
        lines += _line(
            "proofstep_review_queue_oldest_age_seconds",
            _age_seconds(stats.get("oldest_pending")),
            {"queue": slug},
        )

    spend = await budget.status(session, project_id=principal.project)
    lines += [
        "# HELP proofstep_project_spend_usd Server-initiated spend this calendar month.",
        "# TYPE proofstep_project_spend_usd gauge",
        *_line("proofstep_project_spend_usd", float(spend.spent)),
        "# HELP proofstep_project_spend_ratio Share of the monthly ceiling used.",
        "# TYPE proofstep_project_spend_ratio gauge",
        # Absent when unlimited. A 0 here would look like a project spending nothing, which is a
        # different fact from a project that cannot be over budget.
        *_line("proofstep_project_spend_ratio", spend.ratio),
        "# HELP proofstep_project_budget_exhausted 1 when paid rules are being skipped.",
        "# TYPE proofstep_project_budget_exhausted gauge",
        *_line("proofstep_project_budget_exhausted", int(spend.exhausted)),
    ]

    limiter = getattr(request.app.state, "rate_limiter", None)
    lines += [
        "# HELP proofstep_rate_limiter_available 1 when rate limits are actually being enforced.",
        "# TYPE proofstep_rate_limiter_available gauge",
        # Exported because the limiter fails *open*: when its backend is unreachable every request
        # is allowed, which is the right behaviour and is indistinguishable from a quiet period
        # unless something says so.
        *_line(
            "proofstep_rate_limiter_available", None if limiter is None else int(limiter.available)
        ),
    ]

    lines += [
        "# HELP proofstep_rls_enforced 1 when row-level security applies to the connected role.",
        "# TYPE proofstep_rls_enforced gauge",
        *_line("proofstep_rls_enforced", await _rls_gauge(session)),
    ]

    return Response("\n".join(lines) + "\n", media_type=CONTENT_TYPE)


async def _rls_gauge(session: Any) -> int | None:
    """1 enforced, 0 bypassed, absent when it could not be determined.

    Three states rather than two, for the same reason as everything else here: "I could not read the
    catalogue" and "your tenant isolation is off" deserve different responses, and collapsing them
    into 0 would page someone for the wrong thing.
    """
    from proofstep_api.db.rls import role_bypasses_rls  # noqa: PLC0415 — avoids a cycle

    try:
        connection = await session.connection()
        _role, bypasses, _reason = await role_bypasses_rls(connection)
    except Exception:  # a metrics endpoint must not fail on a diagnostic
        return None
    return 0 if bypasses else 1
