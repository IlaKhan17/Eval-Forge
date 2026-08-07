"""Every route, checked against the tenant that does not own the resource.

The value of this file is not the individual assertions — most of them would pass by accident,
because the repository layer filters by `project_id` everywhere. It is the **coverage assertion**:
every operation in the OpenAPI schema must be either exercised here or explicitly excused with a
reason, so a new route cannot quietly escape the sweep. That is what turns "we tested isolation"
into "isolation is tested on everything".

Two rules the sweep enforces, both from the threat model (docs/SECURITY.md §4):

- A foreign resource is **404, never 403**. A 403 confirms the resource exists, which is an
  information leak that turns an unguessable id into a confirmed one.
- A collection endpoint returns the caller's rows only. Never someone else's, and never an error —
  an empty list is the correct answer for a tenant with no data.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from evalforge_api.api.dependencies import get_session
from evalforge_api.main import create_app
from evalforge_api.settings import Settings
from factories import Tenant, make_tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

#: A well-formed id that belongs to nobody. Used where a route takes an id the test cannot easily
#: create, since "unknown id" and "another tenant's id" must both answer 404 — if they differed, the
#: difference itself would be the leak.
ABSENT = uuid.UUID("00000000-0000-4000-8000-000000000000")

#: Operations excused from the sweep, each with the reason. Reviewed rather than implicit: a route
#: missing from both this map and the exercised set fails `test_every_route_is_covered`.
EXCUSED: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "no tenant scope; liveness must not depend on anything",
    ("GET", "/readyz"): "no tenant scope; unauthenticated by design",
    # These create a resource *in the caller's own* tenant from a body that carries no id, so there
    # is no foreign resource to reach. Their isolation is that `project_id` comes from the
    # credential and the body has no field for it — which
    # test_creation_uses_the_credentials_project asserts directly.
    ("POST", "/v1/datasets"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/evaluators"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/experiments"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/online-rules"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/review-queues"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/quality-gate-sets"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/trajectory-policies"): "creates in the caller's tenant; no id in the request",
    ("POST", "/v1/annotations"): "creates in the caller's tenant; target_id is opaque and unscoped",
    ("POST", "/v1/ingest/traces"): "writes to the caller's tenant; covered by test_tenancy.py",
    (
        "POST",
        "/v1/otlp/v1/traces",
    ): "writes to the caller's tenant; covered by test_otlp_receiver.py",
    ("POST", "/v1/trajectory-policies/validate"): "pure validation; touches no stored row",
    ("POST", "/v1/online-rules/run"): "operates on the caller's tenant only; no id in the request",
    ("GET", "/v1/review-queues/health"): "aggregates the caller's own queues",
    ("GET", "/v1/dataset-versions/resolve"): "resolves by slug within the caller's tenant",
    ("POST", "/v1/experiments/compare"): "run ids are validated against the caller's tenant",
    (
        "POST",
        "/v1/datasets/promote-from-trace",
    ): "trace and dataset both resolved in the caller's tenant",
    # Deployment-wide operational records, deliberately not tenant-scoped — a background job spans
    # every project, so a failure belongs to the installation. What they expose is a job name, an
    # exception type, and arguments filtered to an allow-list of ids and limits; the reasoning is in
    # routes/ops.py and db/models/ops.py, and test_dead_letters.py asserts the filtering.
    ("GET", "/v1/ops/queues"): "deployment-wide; the review-queue section is the caller's own",
    ("GET", "/v1/ops/dead-letters"): "deployment-wide operational records, not tenant data",
    (
        "POST",
        "/v1/ops/dead-letters/{dead_letter_id}/resolve",
    ): "deployment-wide operational records, not tenant data",
}


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        yield http


#: Every scope, for both tenants. The intruder is deliberately given *full* permissions: the
#: question this file asks is whether tenancy holds, and a 403 for a missing permission would mask
#: that by answering before the tenancy check runs. It also makes the test stronger — an intruder
#: with every permission in the system still reaches nothing.
ALL_SCOPES = ["ingest", "read", "write", "annotate"]


@pytest_asyncio.fixture
async def owner(session: AsyncSession) -> Tenant:
    return await make_tenant(session, slug="owner", scopes=ALL_SCOPES)


@pytest_asyncio.fixture
async def intruder(session: AsyncSession) -> Tenant:
    return await make_tenant(session, slug="intruder", scopes=ALL_SCOPES)


def auth(tenant: Tenant) -> dict[str, str]:
    return {"authorization": f"Bearer {tenant.token}"}


async def build_resources(client: AsyncClient, tenant: Tenant) -> dict[str, str]:
    """Create one of everything reachable by id, in one tenant.

    Returned as a flat map of names to ids so the parameterized cases below read as claims about a
    route rather than as setup.
    """
    head = auth(tenant)
    ids: dict[str, str] = {}

    dataset = (
        await client.post(
            "/v1/datasets", headers=head, json={"name": "D", "slug": "d", "kind": "golden"}
        )
    ).json()
    ids["dataset"] = dataset["id"]

    version = (
        await client.post(
            f"/v1/datasets/{dataset['id']}/versions", headers=head, json={"version": "v1"}
        )
    ).json()
    ids["dataset_version"] = version["id"]

    # An experiment refuses a draft version, and locking refuses an empty one — so the setup has to
    # add an example and lock. Worth doing rather than working around: the locked path is the one
    # real callers use.
    await client.post(
        f"/v1/dataset-versions/{version['id']}/examples",
        headers=head,
        json={"examples": [{"id": "e1", "input": {"a": 1}, "expected": {"b": 2}}]},
    )
    await client.post(f"/v1/dataset-versions/{version['id']}/lock", headers=head, json={})

    evaluator = (
        await client.post(
            "/v1/evaluators",
            headers=head,
            json={"name": "E", "slug": "e", "evaluator_type": "exact_match"},
        )
    ).json()
    ids["evaluator"] = evaluator["id"]

    evaluator_version = (
        await client.post(
            f"/v1/evaluators/{evaluator['id']}/versions", headers=head, json={"config": {"a": 1}}
        )
    ).json()
    ids["evaluator_version"] = evaluator_version["id"]

    experiment = (
        await client.post(
            "/v1/experiments",
            headers=head,
            json={
                "name": "X",
                "suite_name": "s",
                "dataset_version_id": version["id"],
                "evaluator_version_ids": [evaluator_version["id"]],
            },
        )
    ).json()
    ids["experiment"] = experiment["id"]

    run = (
        await client.post(f"/v1/experiments/{experiment['id']}/runs", headers=head, json={})
    ).json()
    ids["run"] = run["id"]

    policy = (
        await client.post("/v1/trajectory-policies", headers=head, json={"name": "P", "slug": "p"})
    ).json()
    ids["policy"] = policy["id"]

    queue = (
        await client.post("/v1/review-queues", headers=head, json={"name": "Q", "slug": "q"})
    ).json()
    ids["queue"] = queue["id"]

    rule = (
        await client.post(
            "/v1/online-rules",
            headers=head,
            json={
                "name": "R",
                "slug": "r",
                "kind": "deterministic",
                "evaluator_version_id": evaluator_version["id"],
            },
        )
    ).json()
    ids["rule"] = rule["id"]

    return ids


#: (method, path template, resource key). The template is the OpenAPI path, so the coverage check
#: can compare against the schema directly rather than against a hand-kept list.
BY_ID: tuple[tuple[str, str, str, dict[str, Any] | None], ...] = (
    ("GET", "/v1/dataset-versions/{version_id}/examples", "dataset_version", None),
    ("POST", "/v1/dataset-versions/{version_id}/examples", "dataset_version", {"examples": []}),
    ("POST", "/v1/dataset-versions/{version_id}/lock", "dataset_version", {}),
    ("POST", "/v1/datasets/{dataset_id}/versions", "dataset", {"version": "v9"}),
    ("GET", "/v1/evaluator-versions/{version_id}/calibrations", "evaluator_version", None),
    (
        "POST",
        "/v1/evaluator-versions/{version_id}/calibrations",
        "evaluator_version",
        {"n_examples": 1},
    ),
    ("POST", "/v1/evaluators/{evaluator_id}/versions", "evaluator", {"config": {"b": 2}}),
    ("POST", "/v1/experiment-runs/{run_id}/cancel", "run", {}),
    ("POST", "/v1/experiment-runs/{run_id}/complete", "run", {}),
    ("GET", "/v1/experiment-runs/{run_id}/metrics", "run", None),
    ("POST", "/v1/experiment-runs/{run_id}/results", "run", {"results": []}),
    ("POST", "/v1/experiments/{experiment_id}/promote-baseline", "experiment", {}),
    ("POST", "/v1/experiments/{experiment_id}/runs", "experiment", {}),
    ("GET", "/v1/online-rules/{rule_id}/coverage", "rule", None),
    ("POST", "/v1/review-queues/{queue_id}/claim", "queue", {}),
    ("GET", "/v1/review-queues/{queue_id}/items", "queue", None),
    ("POST", "/v1/trajectory-policies/{policy_id}/versions", "policy", {"source_yaml": "x"}),
    ("POST", "/v1/review-assignments/{assignment_id}/complete", "absent", {}),
    ("GET", "/v1/traces/{trace_id}", "absent_trace", None),
)

COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/datasets"),
    ("GET", "/v1/experiments"),
    ("GET", "/v1/online-rules"),
    ("GET", "/v1/review-queues"),
    ("GET", "/v1/traces"),
    ("GET", "/v1/annotations"),
)


class TestForeignResourcesAreNotFound:
    @pytest.mark.parametrize(
        ("method", "template", "key", "body"), BY_ID, ids=[f"{m} {p}" for m, p, _, _ in BY_ID]
    )
    async def test_it_is_404_and_never_403(
        self,
        client: AsyncClient,
        owner: Tenant,
        intruder: Tenant,
        method: str,
        template: str,
        key: str,
        body: dict[str, Any] | None,
    ) -> None:
        """404, never 403, and never 200.

        A 403 tells the caller the resource exists, which converts an unguessable id into a
        confirmed one — the cheapest possible foothold for an attacker enumerating ids.
        """
        ids = await build_resources(client, owner)
        resource = {
            "absent": str(ABSENT),
            "absent_trace": "0" * 32,
            **ids,
        }[key]
        path = template.format(**{template.split("{")[1].split("}", maxsplit=1)[0]: resource})

        response = await client.request(
            method, path, headers=auth(intruder), json=body if body is not None else None
        )
        assert response.status_code == 404, (
            f"{method} {template} answered {response.status_code} to a foreign resource; "
            f"expected 404.\n{response.text[:400]}"
        )
        # And the body must not describe what was not found.
        detail = response.json().get("detail", "")
        assert resource not in detail, f"the 404 body echoed the id: {detail!r}"


class TestCollectionsAreScoped:
    @pytest.mark.parametrize(("method", "path"), COLLECTIONS, ids=[p for _, p in COLLECTIONS])
    async def test_a_tenant_with_no_data_sees_an_empty_result(
        self, client: AsyncClient, owner: Tenant, intruder: Tenant, method: str, path: str
    ) -> None:
        await build_resources(client, owner)
        query = "?target_id=x" if path == "/v1/annotations" else ""
        response = await client.request(method, f"{path}{query}", headers=auth(intruder))

        assert response.status_code == 200, response.text[:300]
        payload = response.json()
        rows = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
        assert rows == [], f"{path} leaked {len(rows)} row(s) to another tenant"


class TestCreationIgnoresTheBody:
    async def test_a_client_supplied_project_id_is_not_honoured(
        self, client: AsyncClient, owner: Tenant, intruder: Tenant
    ) -> None:
        """The tenant comes from the credential, never from the request.

        A client-supplied tenant identifier that reaches a query is among the most common
        multi-tenant breaches, so the input models simply have no field for it. This asserts the
        *absence* holds: sending one anyway must not move the resource.
        """
        created = await client.post(
            "/v1/datasets",
            headers=auth(intruder),
            json={
                "name": "smuggled",
                "slug": "smuggled",
                "kind": "golden",
                "project_id": str(owner.project.id),
                "org_id": str(owner.org.id),
            },
        )
        assert created.status_code in (200, 201), created.text[:300]

        # The owner must not see it.
        theirs = (await client.get("/v1/datasets", headers=auth(owner))).json()
        assert all(row["slug"] != "smuggled" for row in theirs)
        # The intruder must.
        mine = (await client.get("/v1/datasets", headers=auth(intruder))).json()
        assert any(row["slug"] == "smuggled" for row in mine)


class TestCoverage:
    def test_every_route_is_covered(self) -> None:
        """No operation may be absent from both the sweep and the excused map.

        This is the assertion that makes the file worth having. Adding a route with a resource id
        and forgetting to check its isolation fails here, at review time, rather than becoming a
        cross-tenant read in production.
        """
        app = create_app(
            Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32")
        )
        schema = app.openapi()

        declared = {
            (method.upper(), path)
            for path, operations in schema["paths"].items()
            for method in operations
            if method in ("get", "post", "patch", "put", "delete")
        }
        exercised = {(method, template) for method, template, _, _ in BY_ID} | set(COLLECTIONS)
        uncovered = declared - exercised - set(EXCUSED)

        assert not uncovered, (
            "routes with no cross-tenant check and no recorded exemption: "
            f"{sorted(uncovered)}. Add a case to BY_ID/COLLECTIONS, or an entry to EXCUSED with "
            "the reason it needs none."
        )

    def test_no_stale_exemptions(self) -> None:
        # An exemption for a route that no longer exists is a reason nobody will re-examine.
        app = create_app(
            Settings(env="test", jwt_secret="test-secret-value-that-is-long-enough-32")
        )
        declared = {
            (method.upper(), path)
            for path, operations in app.openapi()["paths"].items()
            for method in operations
            if method in ("get", "post", "patch", "put", "delete")
        }
        stale = set(EXCUSED) - declared
        assert not stale, f"exemptions for routes that no longer exist: {sorted(stale)}"

    def test_every_exemption_states_a_reason(self) -> None:
        assert all(reason.strip() for reason in EXCUSED.values())
