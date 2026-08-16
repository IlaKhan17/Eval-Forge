"""Dataset versioning and locking.

Locking is the pivotal operation in the whole reproducibility story. It computes a
content hash over the canonically serialized examples and freezes the version. From
then on, an experiment records both the version id *and* the hash, so a later run can
prove it saw identical data rather than merely claiming the same label.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from proofstep_api.db.models.evaluation import Dataset, DatasetExample, DatasetVersion
from proofstep_api.errors import ConflictError, NotFoundError, UnprocessableError
from proofstep_core.versioning import config_digest
from proofstep_types import Example
from proofstep_types import content_hash as hash_examples


@dataclass(frozen=True, slots=True)
class LockOutcome:
    version: DatasetVersion
    already_locked: bool


class DatasetService:
    def __init__(self, session: AsyncSession, *, project_id: uuid.UUID) -> None:
        self.session = session
        self.project_id = project_id

    # ------------------------------------------------------------------ lookups

    async def get_version(self, version_id: uuid.UUID) -> DatasetVersion:
        version = await self.session.get(DatasetVersion, version_id)
        if version is None or version.project_id != self.project_id:
            # 404 for a foreign row, never 403: a 403 confirms it exists.
            raise NotFoundError("No such dataset version.")
        return version

    async def resolve(self, dataset_slug: str, version_label: str) -> DatasetVersion:
        """Resolve a `slug@label` reference, with `latest-locked` support.

        The CLI addresses datasets by name, not by UUID, because a suite file is
        committed to git and a UUID in one is unreadable and unmergeable.
        """
        dataset = (
            await self.session.execute(
                select(Dataset).where(
                    Dataset.project_id == self.project_id,
                    Dataset.slug == dataset_slug,
                    Dataset.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if dataset is None:
            raise NotFoundError(f"No dataset with slug {dataset_slug!r}.")

        statement = select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
        if version_label == "latest-locked":
            statement = statement.where(DatasetVersion.status == "locked").order_by(
                DatasetVersion.locked_at.desc()
            )
        else:
            statement = statement.where(DatasetVersion.version == version_label)

        version = (await self.session.execute(statement.limit(1))).scalar_one_or_none()
        if version is None:
            raise NotFoundError(f"Dataset {dataset_slug!r} has no version {version_label!r}.")
        return version

    # ------------------------------------------------------------------- writes

    async def append_examples(
        self, version_id: uuid.UUID, examples: list[Example]
    ) -> list[DatasetExample]:
        version = await self.get_version(version_id)
        if version.is_locked:
            raise ConflictError(
                f"Version {version.version!r} was locked at {version.locked_at:%Y-%m-%dT%H:%M:%SZ} "
                "and cannot be modified. Create a new version instead."
            )

        next_ordinal = (
            int(
                (
                    await self.session.execute(
                        select(func.coalesce(func.max(DatasetExample.ordinal), -1)).where(
                            DatasetExample.dataset_version_id == version_id
                        )
                    )
                ).scalar_one()
            )
            + 1
        )

        seen = set(
            (
                await self.session.execute(
                    select(DatasetExample.external_id).where(
                        DatasetExample.dataset_version_id == version_id
                    )
                )
            )
            .scalars()
            .all()
        )

        created: list[DatasetExample] = []
        for offset, example in enumerate(examples):
            if example.id in seen:
                raise ConflictError(
                    f"Example id {example.id!r} already exists in this version. Ids must be "
                    "unique: comparison between experiments matches on them, so a duplicate "
                    "would pair unrelated results."
                )
            seen.add(example.id)
            row = DatasetExample(
                project_id=self.project_id,
                dataset_version_id=version_id,
                ordinal=next_ordinal + offset,
                external_id=example.id,
                input=example.input,
                expected=example.expected,
                example_metadata=example.metadata,
                source_trace_id=example.source_trace_id,
                source_span_id=example.source_span_id,
            )
            self.session.add(row)
            created.append(row)

        await self.session.flush()
        version.example_count = next_ordinal + len(examples)
        return created

    async def lock(self, version_id: uuid.UUID) -> LockOutcome:
        """Freeze a version and record its content hash.

        Idempotent: re-locking returns the same hash rather than erroring, because a
        retried CI step must not fail on a step that already succeeded.
        """
        version = await self.get_version(version_id)

        examples = await self.load_examples(version_id)
        if not examples:
            # A silently empty dataset produces a passing experiment — the worst
            # possible failure mode, since it looks like success.
            raise UnprocessableError(
                "Cannot lock a version with no examples: an empty dataset would produce "
                "a passing experiment that measured nothing."
            )

        digest = bytes.fromhex(hash_examples(examples))

        if version.is_locked:
            if version.content_hash != digest:
                # Should be impossible while the trigger holds; if it happens, the
                # data changed under a lock and the hash is the only thing that knows.
                raise ConflictError(
                    "This version is locked but its content no longer matches the "
                    "recorded hash. The stored data has been altered."
                )
            return LockOutcome(version=version, already_locked=True)

        version.status = "locked"
        version.content_hash = digest
        version.example_count = len(examples)
        version.locked_at = datetime.now(UTC)
        await self.session.flush()
        return LockOutcome(version=version, already_locked=False)

    async def load_examples(self, version_id: uuid.UUID) -> list[Example]:
        rows = (
            (
                await self.session.execute(
                    select(DatasetExample)
                    .where(
                        DatasetExample.project_id == self.project_id,
                        DatasetExample.dataset_version_id == version_id,
                    )
                    .order_by(DatasetExample.ordinal)
                )
            )
            .scalars()
            .all()
        )
        return [
            Example(
                id=row.external_id,
                input=row.input,
                expected=row.expected,
                metadata=row.example_metadata,
                source_trace_id=row.source_trace_id,
                source_span_id=row.source_span_id,
            )
            for row in rows
        ]

    async def clone(self, version_id: uuid.UUID, *, new_label: str) -> DatasetVersion:
        """Copy a version into a new draft, recording lineage.

        This is the sanctioned way to change a locked dataset: fork it, edit the
        fork, lock that. The parent link is what lets someone later see that v4 came
        from v3 rather than appearing from nowhere.
        """
        source = await self.get_version(version_id)
        draft = DatasetVersion(
            project_id=self.project_id,
            dataset_id=source.dataset_id,
            version=new_label,
            status="draft",
            parent_version_id=source.id,
            split=source.split,
        )
        self.session.add(draft)
        await self.session.flush()

        await self.append_examples(draft.id, await self.load_examples(version_id))
        return draft


def config_hash(config: dict[str, object]) -> bytes:
    """Hash everything that could change a score.

    Delegates to `proofstep_core.versioning`, which is the canonical implementation. It
    has to be shared rather than reimplemented: the server stores a calibration against
    this hash and the CLI decides locally whether that calibration still applies, so two
    copies of the algorithm would drift and the drift would show up as silently accepting
    a calibration for a judge that no longer exists.
    """
    return config_digest(config)
