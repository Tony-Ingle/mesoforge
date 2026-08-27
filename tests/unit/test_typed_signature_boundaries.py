"""Static signature/type-hint tests: every public request, service,
application protocol, storage protocol, and concrete repository/
object-store/lock boundary must use the typed ``common.identifiers``
classes (``Digest``, ``ArtifactId``, ``ActivityId``,
``ConfigurationSnapshotId``, ``GridId``, ``RunId``) for ID/digest
parameters and return-object fields, never an unrestricted ``str``
(Codex review t_f569c45c finding 3; final re-review HIGH finding
6/t_1ecb8414: request/protocol/repository signature conformance must be
directly asserted, not only inferred from runtime rejection tests).

This module inspects live ``typing.get_type_hints`` for every public
method across the storage protocols, the concrete PostgreSQL
repositories, the S3 object store, the PostgreSQL idempotency lock, and
``ArtifactService.create_run``/its injected protocols, and asserts that
every parameter/attribute whose name denotes an identifier or digest
(``*_id``, ``*_digest``, ``digest``) is annotated with (or a
``| None``/``tuple[...]`` composition of) one of the typed identifier
classes -- never bare ``str``. Plain ``str`` fields that are genuinely
not identifiers (``storage_uri``, ``media_type``, ``canonical_json``,
``role``, ``code_revision`` -- which has its own dedicated
``validate_code_revision`` runtime check, not a class) are excluded by
an explicit allowlist so this test does not become a blunt "no str
anywhere" rule that would also reject those legitimate fields.
"""

from __future__ import annotations

import inspect
import typing

from mesoforge.application import artifacts as artifacts_module
from mesoforge.application import configuration as configuration_module
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
)
from mesoforge.provenance import lineage as lineage_module
from mesoforge.provenance import services as provenance_services_module
from mesoforge.storage import interfaces as interfaces_module
from mesoforge.storage import s3 as s3_module
from mesoforge.storage.postgres import idempotency_lock as lock_module
from mesoforge.storage.postgres import repositories as repositories_module

_TYPED_IDENTIFIER_CLASSES = (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
)
_TYPED_IDENTIFIER_NAMES = {cls.__name__ for cls in _TYPED_IDENTIFIER_CLASSES}

# Parameter/attribute names that look like an identifier/digest but are
# legitimately plain str -- either because they are validated by a
# dedicated function rather than a class (code_revision), or because
# they genuinely are not an identifier at all despite the name pattern.
_ALLOWED_PLAIN_STR_NAMES = frozenset(
    {
        "code_revision",  # validated by validate_code_revision(), not a typed class
    }
)

_IDENTIFIER_NAME_SUFFIXES = ("_id", "_digest")
_IDENTIFIER_EXACT_NAMES = frozenset({"digest"})

# Aggregate parameter/field names that hold a bare tuple of identifiers
# (not identifier-suffixed themselves) -- final re-review HIGH finding:
# the suffix heuristic alone misses ``ids``/``artifact_nodes``/
# ``activity_nodes``, which is exactly how the unrestricted-str
# regression in ``provenance.lineage`` escaped the original static
# audit. Their annotations must resolve to a typed identifier (or a
# tuple/union composition of one), exactly like a normal identifier
# parameter.
_AGGREGATE_IDENTIFIER_NAMES = frozenset({"ids", "artifact_nodes", "activity_nodes"})

# Parameter/field names that hold an ordered tuple of ``(role, artifact_id)``
# pairs -- the ``role`` element legitimately stays a bare ``str``, but the
# second element of every pair must be a typed identifier. Handled by a
# dedicated shape check (``_annotation_is_typed_role_id_pairs``) rather
# than the generic all-typed-args heuristic, which would otherwise also
# demand ``role`` be typed and produce a false failure.
_ROLE_IDENTIFIER_PAIR_NAMES = frozenset({"ordered_inputs"})


def _looks_like_identifier(name: str) -> bool:
    if name in _ALLOWED_PLAIN_STR_NAMES:
        return False
    if name in _IDENTIFIER_EXACT_NAMES:
        return True
    if name in _AGGREGATE_IDENTIFIER_NAMES or name in _ROLE_IDENTIFIER_PAIR_NAMES:
        return True
    return any(name.endswith(suffix) for suffix in _IDENTIFIER_NAME_SUFFIXES)


def _annotation_uses_typed_identifier(annotation: object) -> bool:
    """True if ``annotation`` is, or is composed of (via ``|``/``Optional``/
    ``tuple[...]``), at least one of the typed identifier classes and no
    bare ``str``."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return annotation in _TYPED_IDENTIFIER_CLASSES
    args = typing.get_args(annotation)
    # str | None, ConfigurationSnapshotId | None, tuple[ArtifactId, ...],
    # tuple[ArtifactId | None, ...], etc: every non-None/non-Ellipsis
    # member must itself resolve to a typed identifier (recursively).
    substantive_args = [a for a in args if a is not type(None) and a is not Ellipsis]
    if not substantive_args:
        return False
    return all(_annotation_uses_typed_identifier(a) for a in substantive_args)


def _annotation_contains_bare_str(annotation: object) -> bool:
    if annotation is str:
        return True
    origin = typing.get_origin(annotation)
    if origin is None:
        return False
    return any(_annotation_contains_bare_str(a) for a in typing.get_args(annotation))


def _annotation_is_typed_role_id_pairs(annotation: object) -> bool:
    """True if ``annotation`` is ``tuple[tuple[str, <TypedId>], ...]``
    (or a ``tuple[tuple[str, <TypedId>], ...] | None``): an ordered
    sequence of ``(role, artifact_id)`` pairs where ``role`` stays a
    bare ``str`` by design but the second element must be one of the
    typed identifier classes with no bare ``str`` fallback."""
    origin = typing.get_origin(annotation)
    if origin is None:
        return False
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    # tuple[tuple[str, ArtifactId], ...] -> args = (tuple[str, ArtifactId], Ellipsis)
    pair_types = [a for a in args if a is not Ellipsis]
    if len(pair_types) != 1:
        return False
    pair_type = pair_types[0]
    if typing.get_origin(pair_type) is not tuple:
        return False
    pair_args = typing.get_args(pair_type)
    if len(pair_args) != 2:
        return False
    role_type, id_type = pair_args
    return role_type is str and id_type in _TYPED_IDENTIFIER_CLASSES


def _assert_identifier_params_typed(
    func: object, *, label: str, owner_globalns: dict[str, object] | None = None
) -> list[str]:
    """Return a list of failure descriptions (empty means fully typed)."""
    failures: list[str] = []
    try:
        hints = typing.get_type_hints(func, include_extras=True, globalns=owner_globalns)
    except Exception as exc:  # pragma: no cover - diagnostic aid only
        failures.append(f"{label}: could not resolve type hints ({exc})")
        return failures

    signature = inspect.signature(func)  # type: ignore[arg-type]
    for param_name in signature.parameters:
        if param_name in {"self", "cls"}:
            continue
        if not _looks_like_identifier(param_name):
            continue
        annotation = hints.get(param_name)
        if annotation is None:
            failures.append(f"{label}: parameter {param_name!r} has no resolvable annotation")
            continue
        if param_name in _ROLE_IDENTIFIER_PAIR_NAMES:
            if not _annotation_is_typed_role_id_pairs(annotation):
                failures.append(
                    f"{label}: parameter {param_name!r} annotation {annotation!r} is not "
                    "tuple[tuple[str, <TypedId>], ...]"
                )
            continue
        if _annotation_contains_bare_str(annotation) or not _annotation_uses_typed_identifier(
            annotation
        ):
            failures.append(
                f"{label}: parameter {param_name!r} annotation {annotation!r} is not a "
                "typed identifier/digest class"
            )
    return failures


def _protocol_methods(protocol_cls: type) -> list[str]:
    return [
        name
        for name, member in vars(protocol_cls).items()
        if callable(member) and not name.startswith("_") and name not in {"__init__"}
    ]


class TestStorageInterfacesProtocolsUseTypedIdentifiers:
    """``storage.interfaces``: every protocol method's ID/digest
    parameters must be typed (final re-review HIGH finding
    6/t_1ecb8414 -- the earlier remediation left this file's
    StoredObject/ConfigurationSnapshotLike/GridDefinitionLike/
    ArtifactObjectStore/StoredObjectRepository/IdempotencyLock
    string-typed)."""

    def test_all_protocol_methods_use_typed_identifiers(self) -> None:
        protocol_classes = [
            interfaces_module.ArtifactObjectStore,
            interfaces_module.StoredObjectRepository,
            interfaces_module.ArtifactRepository,
            interfaces_module.ActivityRepository,
            interfaces_module.ConfigurationRepository,
            interfaces_module.GridRepository,
            interfaces_module.RunRepository,
            interfaces_module.LineageReader,
            interfaces_module.IdempotencyLock,
        ]
        all_failures: list[str] = []
        for protocol_cls in protocol_classes:
            for method_name in _protocol_methods(protocol_cls):
                func = vars(protocol_cls)[method_name]
                label = f"{protocol_cls.__name__}.{method_name}"
                all_failures.extend(
                    _assert_identifier_params_typed(
                        func, label=label, owner_globalns=vars(interfaces_module)
                    )
                )
        assert not all_failures, "\n".join(all_failures)

    def test_value_object_protocols_use_typed_identifier_fields(self) -> None:
        """``StoredObject``, ``ConfigurationSnapshotLike``, and
        ``GridDefinitionLike`` are attribute-only protocols (no
        methods to inspect via signatures); check their annotated
        class-body attributes directly."""
        all_failures: list[str] = []
        for protocol_cls in (
            interfaces_module.StoredObject,
            interfaces_module.ConfigurationSnapshotLike,
            interfaces_module.GridDefinitionLike,
        ):
            hints = typing.get_type_hints(protocol_cls, globalns=vars(interfaces_module))
            for attr_name, annotation in hints.items():
                if not _looks_like_identifier(attr_name):
                    continue
                if _annotation_contains_bare_str(
                    annotation
                ) or not _annotation_uses_typed_identifier(annotation):
                    all_failures.append(
                        f"{protocol_cls.__name__}.{attr_name}: annotation {annotation!r} "
                        "is not a typed identifier/digest class"
                    )
        assert not all_failures, "\n".join(all_failures)


class TestConcretePostgresRepositoriesUseTypedIdentifiers:
    """``storage.postgres.repositories``: every concrete repository's
    public method must declare typed ID/digest parameters (final
    re-review HIGH finding 6/t_1ecb8414 -- grid/config/stored-object
    access, add_derived, artifact lookups, activity methods/lookups,
    and run lookup were all left string-typed)."""

    def test_all_public_repository_methods_use_typed_identifiers(self) -> None:
        repository_classes = [
            repositories_module.PostgresGridRepository,
            repositories_module.PostgresConfigurationRepository,
            repositories_module.PostgresStoredObjectRepository,
            repositories_module.PostgresArtifactRepository,
            repositories_module.PostgresActivityRepository,
            repositories_module.PostgresRunRepository,
        ]
        all_failures: list[str] = []
        for repo_cls in repository_classes:
            for method_name, member in vars(repo_cls).items():
                if method_name.startswith("_") or not callable(member):
                    continue
                label = f"{repo_cls.__name__}.{method_name}"
                all_failures.extend(
                    _assert_identifier_params_typed(
                        member, label=label, owner_globalns=vars(repositories_module)
                    )
                )
        assert not all_failures, "\n".join(all_failures)

    def test_record_dataclasses_use_typed_identifier_fields(self) -> None:
        all_failures: list[str] = []
        for record_cls in (
            repositories_module.GridRecord,
            repositories_module.ConfigurationSnapshotRecord,
            repositories_module.StoredObjectRecord,
        ):
            hints = typing.get_type_hints(record_cls, globalns=vars(repositories_module))
            for attr_name, annotation in hints.items():
                if not _looks_like_identifier(attr_name):
                    continue
                if _annotation_contains_bare_str(
                    annotation
                ) or not _annotation_uses_typed_identifier(annotation):
                    all_failures.append(
                        f"{record_cls.__name__}.{attr_name}: annotation {annotation!r} "
                        "is not a typed identifier/digest class"
                    )
        assert not all_failures, "\n".join(all_failures)


class TestS3ObjectStoreUsesTypedIdentifiers:
    """``storage.s3``: public object-store digest boundaries must be
    typed (final re-review HIGH finding 6/t_1ecb8414)."""

    def test_public_methods_use_typed_digest(self) -> None:
        all_failures: list[str] = []
        for method_name in ("put_if_absent", "get_verified", "exists_verified"):
            func = vars(s3_module.S3ArtifactObjectStore)[method_name]
            all_failures.extend(
                _assert_identifier_params_typed(
                    func,
                    label=f"S3ArtifactObjectStore.{method_name}",
                    owner_globalns=vars(s3_module),
                )
            )
        all_failures.extend(
            _assert_identifier_params_typed(
                s3_module.content_addressed_key,
                label="content_addressed_key",
                owner_globalns=vars(s3_module),
            )
        )
        assert not all_failures, "\n".join(all_failures)

    def test_s3_stored_object_uses_typed_digest_field(self) -> None:
        hints = typing.get_type_hints(s3_module.S3StoredObject, globalns=vars(s3_module))
        annotation = hints["content_digest"]
        assert _annotation_uses_typed_identifier(annotation)
        assert not _annotation_contains_bare_str(annotation)


class TestPostgresIdempotencyLockUsesTypedIdentifiers:
    """``storage.postgres.idempotency_lock``: the public digest boundary
    must be typed (final re-review HIGH finding 6/t_1ecb8414)."""

    def test_acquire_uses_typed_digest(self) -> None:
        failures = _assert_identifier_params_typed(
            lock_module.PostgresIdempotencyLock.acquire,
            label="PostgresIdempotencyLock.acquire",
            owner_globalns=vars(lock_module),
        )
        assert not failures, "\n".join(failures)


class TestArtifactServicePublicBoundaryUsesTypedIdentifiers:
    """``application.artifacts``: ``create_run`` and every injected
    application-side protocol must be typed, not ``str | Typed`` unions
    (final re-review HIGH finding 6/t_1ecb8414: a ``str | Typed`` union
    boundary is not a typed boundary, and the earlier remediation left
    the injected protocols string-typed even though the request models
    were already typed)."""

    def test_create_run_signature_has_no_bare_str_identifier_union(self) -> None:
        hints = typing.get_type_hints(
            artifacts_module.ArtifactService.create_run, globalns=vars(artifacts_module)
        )
        identifier_params = {
            "run_id": RunId,
            "configuration_snapshot_id": ConfigurationSnapshotId,
            "configuration_digest": Digest,
            "environment_digest": Digest,
            "lockfile_digest": Digest,
        }
        failures: list[str] = []
        for param_name, expected_cls in identifier_params.items():
            annotation = hints[param_name]
            if _annotation_contains_bare_str(annotation):
                failures.append(
                    f"create_run parameter {param_name!r} annotation {annotation!r} "
                    f"still permits bare str (expected exactly {expected_cls.__name__})"
                )
            if annotation is not expected_cls:
                failures.append(
                    f"create_run parameter {param_name!r} annotation is {annotation!r}, "
                    f"expected exactly {expected_cls.__name__} (no str union)"
                )
        selected_annotation = hints["selected_input_artifact_ids"]
        if _annotation_contains_bare_str(selected_annotation):
            failures.append(
                f"create_run parameter 'selected_input_artifact_ids' annotation "
                f"{selected_annotation!r} still permits bare str elements"
            )
        assert not failures, "\n".join(failures)

    def test_injected_protocols_use_typed_identifiers(self) -> None:
        protocol_classes = [
            artifacts_module._StoredObjectLike,
            artifacts_module._ObjectStoreLike,
            artifacts_module._ArtifactRepositoryLike,
            artifacts_module._ActivityRepositoryLike,
            artifacts_module._ConfigurationSnapshotLike,
            artifacts_module._ConfigurationRepositoryLike,
            artifacts_module._IdempotencyLockLike,
        ]
        all_failures: list[str] = []
        for protocol_cls in protocol_classes:
            hints = typing.get_type_hints(protocol_cls, globalns=vars(artifacts_module))
            for attr_name, annotation in hints.items():
                if not _looks_like_identifier(attr_name):
                    continue
                if _annotation_contains_bare_str(
                    annotation
                ) or not _annotation_uses_typed_identifier(annotation):
                    all_failures.append(
                        f"{protocol_cls.__name__}.{attr_name}: annotation {annotation!r} "
                        "is not a typed identifier/digest class"
                    )
            for method_name, member in vars(protocol_cls).items():
                if method_name.startswith("_") or not callable(member):
                    continue
                label = f"{protocol_cls.__name__}.{method_name}"
                all_failures.extend(
                    _assert_identifier_params_typed(
                        member, label=label, owner_globalns=vars(artifacts_module)
                    )
                )
        assert not all_failures, "\n".join(all_failures)


class TestConfigurationServiceInjectedProtocolUsesTypedIdentifiers:
    """``application.configuration``: the injected ``_GridRepositoryLike``
    protocol must be typed too (final re-review HIGH finding
    6/t_1ecb8414: every application-layer injected protocol must
    conform, not just ``application/artifacts.py``'s)."""

    def test_grid_repository_like_uses_typed_identifiers(self) -> None:
        failures = _assert_identifier_params_typed(
            configuration_module._GridRepositoryLike.add_if_absent,
            label="_GridRepositoryLike.add_if_absent",
            owner_globalns=vars(configuration_module),
        )
        assert not failures, "\n".join(failures)


class TestProvenanceServicesPublicBoundaryUsesTypedIdentifiers:
    """``provenance.services``: the public, non-Pydantic-mediated
    ``compute_idempotency_digest`` function boundary must be typed (HIGH
    finding: the earlier remediation left ``ordered_inputs``,
    ``configuration_digest``, ``parameters_digest``, ``code_revision``,
    and ``environment_digest`` as unrestricted ``str``/``tuple[str, str]``
    with no boundary reconstruction, so a direct runtime call with
    malformed values succeeded)."""

    def test_compute_idempotency_digest_uses_typed_identifiers(self) -> None:
        failures = _assert_identifier_params_typed(
            provenance_services_module.compute_idempotency_digest,
            label="compute_idempotency_digest",
            owner_globalns=vars(provenance_services_module),
        )
        assert not failures, "\n".join(failures)


class TestProvenanceLineagePublicBoundaryUsesTypedIdentifiers:
    """``provenance.lineage``: every public model/function boundary must
    use typed ``ArtifactId``/``ActivityId``, including aggregate field
    names (``root_artifact_id``, ``artifact_nodes``, ``activity_nodes``)
    that the original suffix-only heuristic could not detect (HIGH
    finding: ``ActivityEdge``, ``LineageEdgeView``, ``LineageGraph``,
    ``build_lineage_graph``, and ``detect_cycle`` were all left
    string-typed)."""

    def test_activity_edge_and_lineage_edge_view_use_typed_fields(self) -> None:
        all_failures: list[str] = []
        for model_cls in (lineage_module.ActivityEdge, lineage_module.LineageEdgeView):
            hints = typing.get_type_hints(model_cls, globalns=vars(lineage_module))
            for attr_name, annotation in hints.items():
                if not _looks_like_identifier(attr_name):
                    continue
                if _annotation_contains_bare_str(
                    annotation
                ) or not _annotation_uses_typed_identifier(annotation):
                    all_failures.append(
                        f"{model_cls.__name__}.{attr_name}: annotation {annotation!r} "
                        "is not a typed identifier/digest class"
                    )
        assert not all_failures, "\n".join(all_failures)

    def test_lineage_graph_uses_typed_fields_including_aggregate_names(self) -> None:
        hints = typing.get_type_hints(lineage_module.LineageGraph, globalns=vars(lineage_module))
        all_failures: list[str] = []
        for attr_name in ("root_artifact_id", "artifact_nodes", "activity_nodes"):
            annotation = hints[attr_name]
            if _annotation_contains_bare_str(annotation) or not _annotation_uses_typed_identifier(
                annotation
            ):
                all_failures.append(
                    f"LineageGraph.{attr_name}: annotation {annotation!r} is not a typed "
                    "identifier class"
                )
        assert not all_failures, "\n".join(all_failures)

    def test_build_lineage_graph_and_detect_cycle_use_typed_identifiers(self) -> None:
        all_failures: list[str] = []
        for func, label in (
            (lineage_module.build_lineage_graph, "build_lineage_graph"),
            (lineage_module.detect_cycle, "detect_cycle"),
        ):
            all_failures.extend(
                _assert_identifier_params_typed(
                    func, label=label, owner_globalns=vars(lineage_module)
                )
            )
        assert not all_failures, "\n".join(all_failures)


class TestStorageLineageReaderUsesTypedIdentifiers:
    """``storage.interfaces.LineageReader`` exposes ``LineageGraph``,
    which must itself be typed (final re-review HIGH finding: this
    storage-facing value boundary remained string-typed via
    ``provenance.lineage`` even though the protocol method signature
    itself already used ``ArtifactId``)."""

    def test_lineage_reader_return_type_is_fully_typed(self) -> None:
        hints = typing.get_type_hints(interfaces_module.LineageGraph, globalns=vars(lineage_module))
        all_failures: list[str] = []
        for attr_name in ("root_artifact_id", "artifact_nodes", "activity_nodes"):
            annotation = hints[attr_name]
            if _annotation_contains_bare_str(annotation) or not _annotation_uses_typed_identifier(
                annotation
            ):
                all_failures.append(
                    f"LineageGraph.{attr_name}: annotation {annotation!r} is not a typed "
                    "identifier class"
                )
        assert not all_failures, "\n".join(all_failures)
