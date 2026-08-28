"""Static signature/type-hint tests: every public request, service,
application protocol, storage protocol, concrete repository/object-store/
lock boundary, and provenance model/function must use the typed
``common.identifiers`` classes (``Digest``, ``ArtifactId``, ``ActivityId``,
``ConfigurationSnapshotId``, ``GridId``, ``RunId``) for ID/digest
parameters and return-object fields, never an unrestricted ``str``
(Codex review t_f569c45c finding 3; final re-review HIGH finding
6/t_1ecb8414: request/protocol/repository signature conformance must be
directly asserted, not only inferred from runtime rejection tests).

Exhaustive-inventory remediation (t_4a21981a): the earlier version of
this module enumerated the classes/functions/methods/fields to inspect
by hand (an explicit ``protocol_classes = [...]`` list, an explicit
``repository_classes = [...]`` list, an explicit S3 method tuple, an
explicit application-protocol list, and explicit provenance
function/model/field lists). A newly added public boundary -- e.g.
``mesoforge.provenance.services.newly_added_public_boundary(artifact_id:
str)`` -- was never discovered by any of those lists, so the suite
stayed green even though the new boundary was unrestricted ``str``.

This module now performs *automatic discovery* instead: it walks every
module in the Phase 0 public service/value boundary scope
(``mesoforge.application``, ``mesoforge.storage``, ``mesoforge.provenance``,
``mesoforge.contracts``) via ``pkgutil.walk_packages``, inspects every
class and every public function actually defined in each discovered
module (not a hand-picked subset), and for each one inspects every
method/``__init__``/annotated field via live ``typing.get_type_hints``.
Adding a new public boundary anywhere in that scope is picked up on the
next test run with zero changes to this file. Internal SQLAlchemy mapped
rows are excluded by boundary kind (``__table__``), not module/class
inventory. The only plain-string exemptions are three exact, documented
catalog labels for which Phase 0 defines no typed value class.
``TestDiscoveryDetectsInjectedMalformedBoundaries``
below proves the discovery mechanism itself is exhaustive: each test
injects a temporary malformed boundary directly onto a real in-scope
module/class (never editing any list in this file) and asserts the scan
reports it.

Plain ``str`` remains legitimate for genuinely non-identifier fields
(``storage_uri``, ``media_type``, ``canonical_json``, ``role``, and
``code_revision``, which has its own dedicated ``validate_code_revision``
runtime check rather than a typed class) via a narrow, documented
allowlist/pattern -- this module does not become a blunt "no str
anywhere" rule that would also reject those legitimate fields.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import sys
import textwrap
import types
import typing

import pytest

import mesoforge.application as _application_pkg
import mesoforge.contracts as _contracts_pkg
import mesoforge.provenance as _provenance_pkg
import mesoforge.storage as _storage_pkg
from mesoforge.common.identifiers import (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    MatchingPolicyId,
    MetricSetId,
    RunId,
    StationId,
    VariableId,
    VerticalDefinitionId,
)

_TYPED_IDENTIFIER_CLASSES = (
    ActivityId,
    ArtifactId,
    ConfigurationSnapshotId,
    Digest,
    GridId,
    RunId,
    VariableId,
    VerticalDefinitionId,
    StationId,
    MatchingPolicyId,
    MetricSetId,
)

# Parameter/attribute names that look like an identifier/digest but are
# legitimately plain str -- validated by a dedicated function rather than
# a typed class (code_revision has no typed class; see
# TestCodeRevisionBoundariesUseSharedRuntimeValidator below, which proves
# every discovered code_revision boundary routes through
# validate_code_revision instead).
_ALLOWED_PLAIN_STR_NAMES = frozenset({"code_revision"})

# Exact catalog labels deliberately represented as strings by
# VariableDefinition. These are not artifact/activity/run identity or
# digest boundaries, and Phase 0 defines no corresponding typed value
# classes. Qualifying each site prevents a similarly named field in any
# other model or module from escaping the audit.
_ALLOWED_PLAIN_STR_SITES: dict[str, str] = {
    "mesoforge.contracts.datasets.VariableLike.variable_id": (
        "catalog variable label; no VariableId value class exists in Phase 0"
    ),
    "mesoforge.contracts.datasets.VariableLike.canonical_unit_id": (
        "catalog unit label; no UnitId value class exists in Phase 0"
    ),
    "mesoforge.contracts.datasets.VariableLike.vertical_definition_id": (
        "catalog vertical-definition label; no typed value class exists in Phase 0"
    ),
    "mesoforge.contracts.observations.RawMetarRecord.icao_id": (
        "external AviationWeather.gov provider ICAO identifier, not a MesoForge "
        "StationId -- station-catalog resolution maps this to StationId separately"
    ),
    "mesoforge.contracts.observations.NormalizedObservation.provider_station_id": (
        "external AviationWeather.gov provider ICAO identifier retained as lineage, "
        "not a MesoForge StationId (see station_id on the same model)"
    ),
    "mesoforge.contracts.verification.MetricRow.unit_id": (
        "catalog unit label; no UnitId value class exists in Phase 0 (matches "
        "VariableLike.canonical_unit_id above)"
    ),
}

# Name patterns that flag a parameter/attribute as identifier/digest-like
# (final re-review HIGH finding: the original suffix-only heuristic
# missed "ids"/"artifact_nodes"/"activity_nodes", which is exactly how
# the unrestricted-str regression in provenance.lineage escaped the
# original static audit). "*_ids" (plural aggregate, e.g.
# selected_input_artifact_ids) is included alongside "*_id" per the
# exhaustive-inventory requirement.
_IDENTIFIER_NAME_SUFFIXES = ("_id", "_ids", "_digest", "_digests")
_IDENTIFIER_EXACT_NAMES = frozenset({"digest", "ids"})
_AGGREGATE_EXACT_NAMES = frozenset({"artifact_nodes", "activity_nodes"})

# The Phase 0 public service/value boundary scope this suite audits.
# pkgutil.walk_packages recurses into every submodule of each package
# automatically, so a newly added module under any of these four
# packages is discovered without editing this file.
_SCOPE_ROOT_PACKAGES: tuple[types.ModuleType, ...] = (
    _application_pkg,
    _storage_pkg,
    _provenance_pkg,
    _contracts_pkg,
)


def _discover_scope_modules() -> dict[str, types.ModuleType]:
    """Every module under the four scoped packages.

    Adding a new module under ``application``, ``storage``, ``provenance``,
    or ``contracts`` makes it appear automatically on the next test run.
    """
    modules: dict[str, types.ModuleType] = {}
    for root in _SCOPE_ROOT_PACKAGES:
        modules[root.__name__] = root
        discovered = pkgutil.walk_packages(root.__path__, prefix=root.__name__ + ".")
        for info in sorted(discovered, key=lambda item: item.name):
            modules[info.name] = importlib.import_module(info.name)
    return dict(sorted(modules.items()))


# Import the audited boundary once during test collection.  Deferring this until a
# test body runs can load the storage native stack after cfgrib/eccodes has already
# decoded messages in an earlier test, an order that segfaults during interpreter
# teardown on Linux.  The inventory remains exhaustive and injected-boundary tests
# still mutate these same live module objects.
_SCOPE_MODULES = _discover_scope_modules()


def _classes_defined_in(module: types.ModuleType) -> list[type]:
    """Every class whose __module__ is this module -- public or
    underscore-prefixed. Underscore-prefixed structural Protocol classes
    (``_StoredObjectLike``, ``_ArtifactRepositoryLike``, etc.) are the
    application layer's injected-boundary shapes and must be inspected
    exactly like a public class; Python's leading-underscore convention
    marks them module-private, not exempt from this boundary audit."""
    classes = [
        member
        for member in vars(module).values()
        if inspect.isclass(member)
        and member.__module__ == module.__name__
        and not hasattr(member, "__table__")
    ]
    return sorted(classes, key=lambda cls: cls.__qualname__)


def _public_functions_defined_in(module: types.ModuleType) -> list[types.FunctionType]:
    """Every public (non-underscore) top-level function defined directly
    in this module. Private module-level helpers (``_edge_sort_key``,
    ``_parameters_digest``, etc.) are internal wiring, not a public
    boundary, and are intentionally excluded -- matching every function
    this suite has ever required typed (``compute_idempotency_digest``,
    ``build_lineage_graph``, ``detect_cycle``) being public."""
    functions = [
        member
        for name, member in vars(module).items()
        if inspect.isfunction(member)
        and member.__module__ == module.__name__
        and not name.startswith("_")
    ]
    return sorted(functions, key=lambda func: func.__qualname__)


def _looks_like_identifier(name: str) -> bool:
    if name in _ALLOWED_PLAIN_STR_NAMES:
        return False
    if name in _IDENTIFIER_EXACT_NAMES or name in _AGGREGATE_EXACT_NAMES:
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


_NOT_PAIR_SHAPE = object()


def _pair_second_element_type(annotation: object) -> object:
    """If ``annotation`` is ``tuple[tuple[str, X], ...]`` (optionally
    wrapped in ``| None``) -- an ordered sequence of ``(role, X)`` pairs,
    the shape ``ordered_inputs`` uses -- return ``X``. Otherwise return
    the ``_NOT_PAIR_SHAPE`` sentinel.

    This is a purely structural check, independent of the field/parameter
    name: it catches a new ``(role, artifact_id)``-shaped aggregate under
    *any* name, not only a name matching ``_ROLE_IDENTIFIER_PAIR_NAMES``
    the way the pre-remediation version required (exhaustive-inventory
    requirement 2). ``tuple[str, ...]`` (a homogeneous variadic tuple of
    plain strings, e.g. ``allowed_dimension_variants``'s inner tuples) is
    correctly NOT a pair shape: its second "element" is the literal
    ``Ellipsis`` variadic marker, not a second, distinct field type.
    """
    origin = typing.get_origin(annotation)
    if origin in {typing.Union, types.UnionType}:
        substantive = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(substantive) == 1:
            return _pair_second_element_type(substantive[0])
        return _NOT_PAIR_SHAPE
    if origin is not tuple:
        return _NOT_PAIR_SHAPE
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    pair_types = [a for a in args if a is not Ellipsis]
    if len(pair_types) != 1:
        return _NOT_PAIR_SHAPE
    pair_type = pair_types[0]
    if typing.get_origin(pair_type) is not tuple:
        return _NOT_PAIR_SHAPE
    pair_args = typing.get_args(pair_type)
    if len(pair_args) != 2:
        return _NOT_PAIR_SHAPE
    role_type, second_type = pair_args
    if role_type is not str or second_type is Ellipsis:
        return _NOT_PAIR_SHAPE
    return second_type


def _scan_named_annotation(name: str, annotation: object, label: str, failures: list[str]) -> None:
    """Check one named parameter/attribute's annotation, appending a
    failure description to ``failures`` if it violates the typed
    identifier/digest boundary. Handles the ``(role, id)`` pair shape
    structurally first (independent of ``name``), then falls back to the
    name-pattern-based identifier/digest/aggregate check."""
    if annotation is None:
        return
    if f"{label}.{name}" in _ALLOWED_PLAIN_STR_SITES:
        return

    pair_second = _pair_second_element_type(annotation)
    if pair_second is not _NOT_PAIR_SHAPE:
        if (
            pair_second is str
            or _annotation_contains_bare_str(pair_second)
            or not _annotation_uses_typed_identifier(pair_second)
        ):
            failures.append(
                f"{label}: {name!r} annotation {annotation!r} is a (role, id)-shaped "
                f"tuple whose second element {pair_second!r} is not a typed identifier "
                "class (role, the first element, legitimately stays bare str)"
            )
        return

    if not _looks_like_identifier(name):
        return
    if _annotation_contains_bare_str(annotation) or not _annotation_uses_typed_identifier(
        annotation
    ):
        failures.append(
            f"{label}: {name!r} annotation {annotation!r} is not a typed identifier/digest class"
        )


def _scan_callable(func: object, label: str, failures: list[str]) -> None:
    module = sys.modules.get(getattr(func, "__module__", None))
    globalns = vars(module) if module is not None else None
    try:
        hints = typing.get_type_hints(func, include_extras=True, globalns=globalns)
    except Exception as exc:  # pragma: no cover - diagnostic aid only
        failures.append(f"{label}: could not resolve type hints ({exc})")
        return
    try:
        signature = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:  # pragma: no cover - diagnostic aid only
        failures.append(f"{label}: could not resolve signature ({exc})")
        return

    for param_name in signature.parameters:
        if param_name in {"self", "cls"}:
            continue
        if param_name not in hints:
            if _looks_like_identifier(param_name):
                failures.append(f"{label}: parameter {param_name!r} has no resolvable annotation")
            continue
        _scan_named_annotation(param_name, hints[param_name], label, failures)

    # Return-type checking is limited to the structural (role, id) pair
    # shape: unlike parameters/fields, a bare return annotation has no
    # name to pattern-match against, and plenty of legitimate functions
    # return a bare non-identifier str (e.g. content_addressed_key's S3
    # key) or a manifest/record class whose own fields are independently
    # discovered and scanned as a class in their own right.
    if "return" in hints:
        return_annotation = hints["return"]
        pair_second = _pair_second_element_type(return_annotation)
        if pair_second is not _NOT_PAIR_SHAPE and (
            pair_second is str
            or _annotation_contains_bare_str(pair_second)
            or not _annotation_uses_typed_identifier(pair_second)
        ):
            failures.append(
                f"{label}: return annotation {return_annotation!r} is a (role, id)-shaped "
                f"tuple whose second element {pair_second!r} is not a typed identifier class"
            )


def _scan_class(cls: type, failures: list[str]) -> None:
    module = sys.modules.get(cls.__module__)
    globalns = vars(module) if module is not None else None
    try:
        hints = typing.get_type_hints(cls, include_extras=True, globalns=globalns)
    except Exception as exc:  # pragma: no cover - diagnostic aid only
        failures.append(f"{cls.__qualname__}: could not resolve class type hints ({exc})")
        hints = {}
    for attr_name, annotation in hints.items():
        _scan_named_annotation(
            attr_name, annotation, f"{cls.__module__}.{cls.__qualname__}", failures
        )

    own_members = vars(cls)
    init = own_members.get("__init__")
    if init is not None and init is not object.__init__:
        _scan_callable(init, f"{cls.__module__}.{cls.__qualname__}.__init__", failures)

    for name, member in own_members.items():
        if name == "__init__" or (name.startswith("__") and name.endswith("__")):
            continue
        if isinstance(member, (classmethod, staticmethod)):
            member = member.__func__
        if inspect.isfunction(member):
            _scan_callable(member, f"{cls.__module__}.{cls.__qualname__}.{name}", failures)


def collect_boundary_failures(
    modules: dict[str, types.ModuleType] | None = None,
) -> list[str]:
    """Run the full automatic discovery + scan over ``modules`` (defaults
    to the live Phase 0 scope) and return every violation description.
    Exposed as a module-level function (not buried in a test method) so
    ``TestDiscoveryDetectsInjectedMalformedBoundaries`` can call it after
    injecting a temporary malformed boundary onto a real module."""
    if modules is None:
        modules = _SCOPE_MODULES
    failures: list[str] = []
    for module_name in sorted(modules):
        module = modules[module_name]
        for cls in _classes_defined_in(module):
            _scan_class(cls, failures)
        for func in _public_functions_defined_in(module):
            _scan_callable(func, f"{module.__name__}.{func.__name__}", failures)
    return sorted(failures)


def _discover_code_revision_sites() -> list[tuple[str, object]]:
    """Every discovered class field / function-or-method parameter named
    exactly ``code_revision`` across the scope, as ``(label, object)``
    pairs. Automatic, name-based discovery -- not an enumerated list of
    "the functions that happen to take code_revision today"."""
    sites: list[tuple[str, object]] = []
    modules = _SCOPE_MODULES
    for module_name in sorted(modules):
        module = modules[module_name]
        for cls in _classes_defined_in(module):
            globalns = vars(module)
            try:
                hints = typing.get_type_hints(cls, include_extras=True, globalns=globalns)
            except Exception:  # pragma: no cover - diagnostic aid only
                hints = {}
            # Protocol methods only declare the static contract and cannot
            # execute validation. Their concrete implementations are
            # discovered independently and must validate at runtime.
            if getattr(cls, "_is_protocol", False):
                continue
            if "code_revision" in hints:
                sites.append((f"{cls.__qualname__}.code_revision (field)", cls))
            for name, member in vars(cls).items():
                if name.startswith("__") and name != "__init__":
                    continue
                if isinstance(member, (classmethod, staticmethod)):
                    member = member.__func__
                if not inspect.isfunction(member):
                    continue
                try:
                    params = inspect.signature(member).parameters
                except (TypeError, ValueError):  # pragma: no cover - diagnostic aid only
                    continue
                if "code_revision" in params:
                    sites.append((f"{cls.__qualname__}.{name}", member))
        for func in _public_functions_defined_in(module):
            try:
                params = inspect.signature(func).parameters
            except (TypeError, ValueError):  # pragma: no cover - diagnostic aid only
                continue
            if "code_revision" in params:
                sites.append((f"{module.__name__}.{func.__name__}", func))
    return sorted(sites, key=lambda site: site[0])


def _code_revision_boundary_has_runtime_validator(obj: object) -> bool:
    """True if ``obj`` (a class carrying a ``code_revision`` field, or a
    function/method taking a ``code_revision`` parameter) routes that
    value through ``validate_code_revision`` -- via a Pydantic
    field/model validator for a class, or a direct call for a plain
    callable. Uses source inspection as a generic, automatable proxy for
    "calls the shared validator" that works for a boundary added after
    this file was last edited, not just the ones enumerated today."""
    if inspect.isclass(obj):
        decorators = getattr(obj, "__pydantic_decorators__", None)
        if decorators is not None:
            for validator in decorators.field_validators.values():
                if "code_revision" in validator.info.fields:
                    try:
                        source = inspect.getsource(validator.func)
                    except (OSError, TypeError):  # pragma: no cover - diagnostic aid only
                        source = ""
                    if _source_calls_code_revision_validator(source):
                        return True
            for validator in decorators.model_validators.values():
                try:
                    source = inspect.getsource(validator.func)
                except (OSError, TypeError):  # pragma: no cover - diagnostic aid only
                    source = ""
                if _source_calls_code_revision_validator(source):
                    return True
        init = vars(obj).get("__init__")
        if init is not None:
            try:
                source = inspect.getsource(init)
            except (OSError, TypeError):  # pragma: no cover - diagnostic aid only
                source = ""
            if _source_calls_code_revision_validator(source):
                return True
        return False

    try:
        source = inspect.getsource(obj)  # type: ignore[arg-type]
    except (OSError, TypeError):  # pragma: no cover - diagnostic aid only
        return False
    return _source_calls_code_revision_validator(source)


def _source_calls_code_revision_validator(source: str) -> bool:
    """Return whether executable source contains a direct call to the
    shared validator; comments, docstrings, and mere references do not
    count as runtime validation."""
    if not source:
        return False
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:  # pragma: no cover - diagnostic aid only
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        is_validator = (
            isinstance(node.func, ast.Name) and node.func.id == "validate_code_revision"
        ) or (isinstance(node.func, ast.Attribute) and node.func.attr == "validate_code_revision")
        argument = node.args[0]
        uses_boundary_value = (
            isinstance(argument, ast.Name) and argument.id in {"code_revision", "value"}
        ) or (isinstance(argument, ast.Attribute) and argument.attr == "code_revision")
        if is_validator and uses_boundary_value:
            return True
    return False


class TestExhaustivePublicBoundaryInventory:
    """The core exhaustive scan: every class/function automatically
    discovered under the Phase 0 public service/value boundary scope
    must use typed identifiers/digests for every ID/digest-shaped
    parameter, attribute, and (role, id) pair aggregate."""

    def test_no_bare_str_identifier_digest_boundaries(self) -> None:
        failures = collect_boundary_failures()
        assert not failures, "\n".join(failures)

    def test_discovery_covers_every_scoped_module(self) -> None:
        """Discovery cannot silently omit a newly added scoped module."""
        all_modules: set[str] = set()
        for root in _SCOPE_ROOT_PACKAGES:
            all_modules.add(root.__name__)
            for info in pkgutil.walk_packages(root.__path__, prefix=root.__name__ + "."):
                all_modules.add(info.name)

        discovered = set(_SCOPE_MODULES)
        assert discovered == all_modules

    def test_plain_string_site_allowlist_is_exact_and_live(self) -> None:
        """Every qualified exception must still resolve to a bare-string
        field; stale, misspelled, or module-wide exemptions are forbidden."""
        discovered_sites: set[str] = set()
        for module in _SCOPE_MODULES.values():
            for cls in _classes_defined_in(module):
                hints = typing.get_type_hints(cls, include_extras=True, globalns=vars(module))
                for field_name, annotation in hints.items():
                    site = f"{cls.__module__}.{cls.__qualname__}.{field_name}"
                    if site in _ALLOWED_PLAIN_STR_SITES and _annotation_contains_bare_str(
                        annotation
                    ):
                        discovered_sites.add(site)

        assert discovered_sites == set(_ALLOWED_PLAIN_STR_SITES)


class TestCodeRevisionBoundariesUseSharedRuntimeValidator:
    """Revision-bearing public boundaries: ``code_revision`` has no typed
    class (it is validated by ``validate_code_revision`` instead), so it
    is excepted from the bare-str check above -- but every discovered
    ``code_revision`` field/parameter must be proven to actually route
    through that shared validator, automatically, not by trusting the
    exception list alone.

    ``source_revision`` (``SourceIdentity``/``SourceRegistrationRequest``)
    is a deliberately distinct, free-form external-source revision label
    (e.g. ``"v1"``), not a 40-hex-character Git SHA, so it is intentionally
    not part of this ``validate_code_revision``-governed family and is not
    inventoried here.
    """

    def test_every_discovered_code_revision_boundary_is_validated(self) -> None:
        sites = _discover_code_revision_sites()
        # If this ever comes back empty, the discovery mechanism itself
        # broke silently -- code_revision is a real, load-bearing field
        # in this codebase today.
        assert sites, "expected at least one discovered code_revision boundary"
        failures = [
            label for label, obj in sites if not _code_revision_boundary_has_runtime_validator(obj)
        ]
        assert not failures, "\n".join(failures)


class TestDiscoveryDetectsInjectedMalformedBoundaries:
    """Regression proving discovery itself is exhaustive, not another
    enumerated list (exhaustive-inventory requirement 3): each test
    injects a temporary malformed public boundary directly onto a real
    in-scope module/class -- never editing any list in this file -- and
    asserts the automatic scan reports it. If ``collect_boundary_failures``
    were ever reverted to a hand-picked inventory, every test below would
    fail, because the injected boundary is never in any such list.
    """

    def test_injected_malformed_public_function_is_discovered(self) -> None:
        import mesoforge.provenance.services as services_module

        def newly_added_public_boundary(artifact_id: str) -> str:  # pragma: no cover
            return artifact_id

        newly_added_public_boundary.__module__ = services_module.__name__
        services_module.newly_added_public_boundary = newly_added_public_boundary
        try:
            failures = collect_boundary_failures()
        finally:
            del services_module.newly_added_public_boundary

        assert any(
            "newly_added_public_boundary" in failure and "artifact_id" in failure
            for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_protocol_method_is_discovered(self) -> None:
        from mesoforge.storage import interfaces as interfaces_module

        def rogue_lookup(self: object, artifact_id: str) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_lookup.__module__ = interfaces_module.__name__
        interfaces_module.ArtifactRepository.rogue_lookup = rogue_lookup
        try:
            failures = collect_boundary_failures()
        finally:
            del interfaces_module.ArtifactRepository.rogue_lookup

        assert any(
            "ArtifactRepository.rogue_lookup" in failure and "artifact_id" in failure
            for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_classmethod_is_discovered(self) -> None:
        """Descriptor-wrapped methods are boundaries too; discovery must
        not be limited to members for which ``inspect.isfunction`` is true."""
        from mesoforge.storage import interfaces as interfaces_module

        def rogue_lookup(cls: type, artifact_id: str) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_lookup.__module__ = interfaces_module.__name__
        interfaces_module.ArtifactRepository.rogue_class_lookup = classmethod(rogue_lookup)
        try:
            failures = collect_boundary_failures()
        finally:
            delattr(interfaces_module.ArtifactRepository, "rogue_class_lookup")

        assert any(
            "ArtifactRepository.rogue_class_lookup" in failure and "artifact_id" in failure
            for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_staticmethod_is_discovered(self) -> None:
        from mesoforge.storage import interfaces as interfaces_module

        def rogue_lookup(artifact_id: str) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_lookup.__module__ = interfaces_module.__name__
        interfaces_module.ArtifactRepository.rogue_static_lookup = staticmethod(rogue_lookup)
        try:
            failures = collect_boundary_failures()
        finally:
            delattr(interfaces_module.ArtifactRepository, "rogue_static_lookup")

        assert any(
            "ArtifactRepository.rogue_static_lookup" in failure and "artifact_id" in failure
            for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_model_field_is_discovered(self) -> None:
        from pydantic import BaseModel, ConfigDict

        from mesoforge.contracts import artifacts as artifacts_module

        class _RogueManifest(BaseModel):
            model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

            artifact_id: str

        _RogueManifest.__module__ = artifacts_module.__name__
        artifacts_module._RogueManifest = _RogueManifest
        try:
            failures = collect_boundary_failures()
        finally:
            del artifacts_module._RogueManifest

        assert any(
            "_RogueManifest" in failure and "artifact_id" in failure for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_aggregate_ids_collection_is_discovered(self) -> None:
        import mesoforge.provenance.lineage as lineage_module

        def rogue_bulk_lookup(*, ids: tuple[str, ...]) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_bulk_lookup.__module__ = lineage_module.__name__
        lineage_module.rogue_bulk_lookup = rogue_bulk_lookup
        try:
            failures = collect_boundary_failures()
        finally:
            del lineage_module.rogue_bulk_lookup

        assert any("rogue_bulk_lookup" in failure and "'ids'" in failure for failure in failures), (
            "\n".join(failures)
        )

    @pytest.mark.parametrize(
        "field_name",
        ("selected_artifact_ids", "artifact_nodes", "activity_nodes"),
    )
    def test_injected_malformed_aggregate_model_field_is_discovered(self, field_name: str) -> None:
        from pydantic import ConfigDict, create_model

        from mesoforge.contracts import provenance as provenance_module

        rogue_model = create_model(
            "_RogueAggregate",
            __config__=ConfigDict(extra="forbid", frozen=True, strict=True),
            __module__=provenance_module.__name__,
            **{field_name: (tuple[str, ...], ...)},
        )
        provenance_module._RogueAggregate = rogue_model
        try:
            failures = collect_boundary_failures()
        finally:
            delattr(provenance_module, "_RogueAggregate")

        assert any(
            "_RogueAggregate" in failure and field_name in failure for failure in failures
        ), "\n".join(failures)

    def test_injected_malformed_role_id_pair_is_discovered(self) -> None:
        """Structural (role, id) pair detection: a new aggregate whose
        second tuple element is a bare str must be caught even under a
        field name (``entries``) that matches no identifier heuristic at
        all -- proving the pair-shape check is independent of naming."""
        import mesoforge.provenance.services as services_module

        def rogue_pair_consumer(
            *, entries: tuple[tuple[str, str], ...]
        ) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_pair_consumer.__module__ = services_module.__name__
        services_module.rogue_pair_consumer = rogue_pair_consumer
        try:
            failures = collect_boundary_failures()
        finally:
            del services_module.rogue_pair_consumer

        assert any(
            "rogue_pair_consumer" in failure and "entries" in failure for failure in failures
        ), "\n".join(failures)

    def test_injected_optional_malformed_role_id_pair_is_discovered(self) -> None:
        import mesoforge.provenance.services as services_module

        def rogue_optional_pair_consumer(
            *, entries: tuple[tuple[str, str], ...] | None
        ) -> None:  # pragma: no cover
            raise NotImplementedError

        rogue_optional_pair_consumer.__module__ = services_module.__name__
        services_module.rogue_optional_pair_consumer = rogue_optional_pair_consumer
        try:
            failures = collect_boundary_failures()
        finally:
            del services_module.rogue_optional_pair_consumer

        assert any(
            "rogue_optional_pair_consumer" in failure and "entries" in failure
            for failure in failures
        ), "\n".join(failures)

    def test_injected_code_revision_boundary_without_validator_is_discovered(self) -> None:
        import mesoforge.provenance.services as services_module

        def rogue_revision_consumer(*, code_revision: str) -> str:  # pragma: no cover
            return code_revision

        rogue_revision_consumer.__module__ = services_module.__name__
        services_module.rogue_revision_consumer = rogue_revision_consumer
        try:
            sites = _discover_code_revision_sites()
            matching = [label for label, obj in sites if "rogue_revision_consumer" in label]
            assert matching, "expected the injected code_revision boundary to be discovered"
            assert not _code_revision_boundary_has_runtime_validator(
                services_module.rogue_revision_consumer
            )
        finally:
            del services_module.rogue_revision_consumer

    def test_injected_code_revision_model_without_validator_is_discovered(self) -> None:
        from pydantic import BaseModel, ConfigDict

        from mesoforge.contracts import provenance as provenance_module

        class _RogueRevisionModel(BaseModel):
            model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

            code_revision: str

        _RogueRevisionModel.__module__ = provenance_module.__name__
        provenance_module._RogueRevisionModel = _RogueRevisionModel
        try:
            sites = _discover_code_revision_sites()
            matching = [(label, obj) for label, obj in sites if "_RogueRevisionModel" in label]
            assert matching, "expected the injected revision model to be discovered"
            assert all(
                not _code_revision_boundary_has_runtime_validator(obj) for _, obj in matching
            )
        finally:
            delattr(provenance_module, "_RogueRevisionModel")
