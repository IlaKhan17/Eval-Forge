"""The permission matrix, and the principal types that are checked against it.

One table, consulted by one dependency. Scattering `if role == "admin"` through
route handlers is how an endpoint quietly ends up with the wrong check, and there
is no way to audit a policy that only exists as scattered conditionals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Permission(StrEnum):
    PROJECT_READ = "project.read"
    PROJECT_MANAGE = "project.manage"
    KEYS_MANAGE = "keys.manage"
    MEMBERS_MANAGE = "members.manage"
    DATASET_WRITE = "dataset.write"
    DATASET_LOCK = "dataset.lock"
    EVALUATOR_WRITE = "evaluator.write"
    POLICY_WRITE = "policy.write"
    GATE_WRITE = "gate.write"
    EXPERIMENT_RUN = "experiment.run"
    RESULTS_WRITE = "results.write"
    ANNOTATION_WRITE = "annotation.write"
    TRACE_INGEST = "trace.ingest"
    AUDIT_READ = "audit.read"


_VIEWER = frozenset({Permission.PROJECT_READ})
_REVIEWER = _VIEWER | {Permission.ANNOTATION_WRITE}
_DEVELOPER = _REVIEWER | {
    Permission.DATASET_WRITE,
    Permission.DATASET_LOCK,
    Permission.EVALUATOR_WRITE,
    Permission.POLICY_WRITE,
    Permission.GATE_WRITE,
    Permission.EXPERIMENT_RUN,
    Permission.RESULTS_WRITE,
    Permission.TRACE_INGEST,
}
_ADMIN = _DEVELOPER | {
    Permission.PROJECT_MANAGE,
    Permission.KEYS_MANAGE,
    Permission.MEMBERS_MANAGE,
    Permission.AUDIT_READ,
}
_OWNER = _ADMIN

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "owner": frozenset(_OWNER),
    "admin": frozenset(_ADMIN),
    "developer": frozenset(_DEVELOPER),
    "reviewer": frozenset(_REVIEWER),
    "viewer": frozenset(_VIEWER),
}

# An API key's scopes cap what it can do, independently of any role. An ingest-only
# key that leaks from a container image must not be able to read the traces back.
SCOPE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "ingest": frozenset({Permission.TRACE_INGEST}),
    "read": frozenset({Permission.PROJECT_READ}),
    "write": frozenset(
        {
            Permission.DATASET_WRITE,
            Permission.DATASET_LOCK,
            Permission.EVALUATOR_WRITE,
            Permission.POLICY_WRITE,
            Permission.GATE_WRITE,
            Permission.EXPERIMENT_RUN,
            Permission.RESULTS_WRITE,
            Permission.PROJECT_READ,
        }
    ),
    # Deliberately its own scope rather than part of `write`. An annotation is *ground
    # truth* — the thing judge calibration is measured against and golden datasets are
    # promoted from — so the authority to create one is not the same as the authority to
    # register an evaluator or run an experiment. Keeping them separate means a CI key
    # cannot inject labels, and a key handed to an annotation tool cannot rewrite gates.
    "annotate": frozenset({Permission.ANNOTATION_WRITE, Permission.PROJECT_READ}),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and what they may do.

    `project_id` is resolved from the credential, never from the request body. A
    client-supplied tenant id that reaches a query is one of the most common
    multi-tenant breaches, so the field simply does not exist on the input models.
    """

    kind: str  # "user" | "api_key"
    id: str
    permissions: frozenset[Permission]
    org_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    role: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def project(self) -> uuid.UUID:
        """The project this credential is scoped to, narrowed.

        Raises rather than returning None. Every caller that reaches here has already passed
        a guard requiring a project-scoped credential, so threading `UUID | None` further
        forces a `type: ignore` at each use site — and a silenced type error at twenty call
        sites is how a genuine None eventually slips into a tenant filter.
        """
        if self.project_id is None:
            msg = (
                "principal is not project-scoped; a route guard should have rejected this "
                "request before reaching a tenant-scoped query"
            )
            raise RuntimeError(msg)
        return self.project_id

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def audit_actor(self) -> tuple[str, str]:
        return self.kind, self.id


def permissions_for_role(role: str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def permissions_for_scopes(scopes: list[str] | tuple[str, ...]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for scope in scopes:
        granted |= SCOPE_PERMISSIONS.get(scope, frozenset())
    return frozenset(granted)
