"""Bounded MRMS acquisition, immutable artifact lineage and read-only offline replay."""

from __future__ import annotations

import copy
import errno
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.prepared_mrms import (
    PRODUCTS,
    MRMSContractError,
    MRMSHourResolver,
    MRMSProviderError,
    MRMSUnavailableError,
    acquire_bundle,
    load_bundle,
    register_bundle,
    replay_registered,
)
from mesoforge.catalog.configuration import load_configuration_source
from mesoforge.common.errors import IntegrityError
from mesoforge.common.identifiers import ArtifactId, Digest
from mesoforge.observations.sources.mrms import source_url
from tests.support.in_memory_uow import (
    InMemoryIdempotencyLock,
    InMemoryObjectStore,
    InMemoryUnitOfWorkFactory,
)
from tests.support.phase1_fixture_transports import FakeHttpResponse, FixedClock
from tests.unit.observations.test_mrms import make_mrms_message

TIME = datetime(2026, 9, 24, 12, tzinfo=UTC)
ACQUIRED = TIME + timedelta(hours=2)
ROOT = Path(__file__).resolve().parents[3]


class Transport:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append(url)
        product = next(
            product for product in PRODUCTS if url.rsplit("/", 1)[1].startswith(f"MRMS_{product}_")
        )
        return FakeHttpResponse(
            200,
            {
                "ETag": '"source-object"',
                "Set-Cookie": "must-not-retain",
                "Content-Type": "application/gzip",
            },
            self.payloads[product],
        )

    def head(self, *args: Any, **kwargs: Any) -> FakeHttpResponse:
        raise AssertionError("MRMS should acquire exactly the three specified objects")


def bundle(tmp_path: Path, *, amount: float = 2.5) -> tuple[Path, dict[str, Any], Transport]:
    payloads = {
        product: make_mrms_message(product=product, values=(amount if index == 0 else 0.75,) * 4)
        for index, product in enumerate(PRODUCTS)
    }
    transport = Transport(payloads)
    directory = tmp_path / "raw"
    metadata = acquire_bundle(
        directory, product_time=TIME, transport=transport, clock=FixedClock(ACQUIRED)
    )
    return directory, metadata, transport


@pytest.fixture
def infrastructure() -> tuple[ArtifactService, Any, Any, Any]:
    factory = InMemoryUnitOfWorkFactory()
    objects = InMemoryObjectStore()
    configuration, _ = load_configuration_source(base_path=ROOT / "configs/base.yaml")
    snapshot = ConfigurationService(factory).register(configuration)
    artifacts = ArtifactService(
        unit_of_work_factory=factory,
        object_store=objects,
        idempotency_lock=InMemoryIdempotencyLock(),
    )
    return artifacts, factory, objects, snapshot


def register(
    directory: Path, infrastructure: tuple[Any, ...], **coordinate: float
) -> dict[str, Any]:
    artifacts, _, _, snapshot = infrastructure
    return register_bundle(
        directory,
        latitude=coordinate.get("latitude", 45.005),
        longitude=coordinate.get("longitude", -93.265),
        artifacts=artifacts,
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
    )


def test_fixed_hour_acquisition_retains_original_bytes_and_only_three_sources(
    tmp_path: Path,
) -> None:
    directory, metadata, transport = bundle(tmp_path)
    assert transport.calls == [source_url(product, TIME) for product in PRODUCTS]
    replay, raw = load_bundle(directory)
    assert replay == metadata
    for product in PRODUCTS:
        source = metadata["sources"][product]
        assert raw[product] == transport.payloads[product]
        assert source["content_digest"] == str(Digest.of_bytes(raw[product]))
        assert source["acquired_at"] == ACQUIRED.isoformat()
        assert source["parsed_metadata"]["product_time"] == "2026-09-24T12:00:00Z"
        assert "set-cookie" not in source["response_identity"]
    with pytest.raises(FileExistsError):
        acquire_bundle(
            directory, product_time=TIME, transport=transport, clock=FixedClock(ACQUIRED)
        )
    assert len(transport.calls) == 3


def test_archive_acquisition_retains_exact_official_urls_and_replays_offline(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {product: make_mrms_message(product) for product in PRODUCTS}
    transport = Transport(payloads)
    directory = tmp_path / "archive"
    metadata = acquire_bundle(
        directory,
        product_time=TIME,
        transport=transport,
        clock=FixedClock(ACQUIRED),
        archive=True,
    )
    expected = [
        f"https://noaa-mrms-pds.s3.amazonaws.com/CONUS/{product}_00.00/20260924/"
        f"MRMS_{product}_00.00_20260924-120000.grib2.gz"
        for product in PRODUCTS
    ]
    assert transport.calls == expected
    assert [metadata["sources"][product]["url"] for product in PRODUCTS] == expected

    def no_network() -> Any:
        raise AssertionError("Retained archive replay attempted provider access")

    monkeypatch.setattr("mesoforge.application.prepared_mrms.default_transport", no_network)
    assert load_bundle(directory) == (metadata, payloads)
    first = register(directory, infrastructure)
    replay = replay_registered(
        ArtifactId(first["extraction_artifact_id"]), artifacts=infrastructure[0]
    )
    assert replay["extraction"] == first["extraction"]
    assert replay["provider_calls"] == replay["storage_writes"] == 0
    metadata["sources"][PRODUCTS[0]]["url"] = expected[0].replace("20260924/", "20260923/")
    (directory / "manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="locator"):
        load_bundle(directory)


def test_immutable_registration_repeat_and_offline_replay_are_exact(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _, transport = bundle(tmp_path)
    first = register(directory, infrastructure)
    assert register(directory, infrastructure) == first
    artifacts, factory, objects, _ = infrastructure
    assert len(factory.artifacts) == 4
    assert len(factory.activities) == 1
    extraction = first["extraction"]
    assert extraction["qpe"]["value"]["value"] == 2.5
    assert extraction["qpe"]["temporal"]["interval_start"] == "2026-09-24T11:00:00Z"
    for reference in extraction["raw_sources"].values():
        raw_manifest, raw = artifacts.load_verified_payload(ArtifactId(reference["artifact_id"]))
        assert raw_manifest.content_digest == Digest.of_bytes(raw)
        assert raw_manifest.availability.available_at == ACQUIRED
    state = copy.deepcopy((factory.artifacts, factory.activities, objects.objects))

    def no_network() -> Any:
        raise AssertionError("Offline paths must not construct network transport")

    monkeypatch.setattr("mesoforge.application.prepared_mrms.default_transport", no_network)
    repeated = replay_registered(ArtifactId(first["extraction_artifact_id"]), artifacts=artifacts)
    assert repeated["offline_replay_identical"] is True
    assert repeated["extraction"] == extraction
    assert repeated["provider_calls"] == repeated["storage_writes"] == 0
    assert state == (factory.artifacts, factory.activities, objects.objects)
    assert len(transport.calls) == 3


def test_coordinates_reuse_raw_sources_and_revisions_preserve_original(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    directory, _, transport = bundle(tmp_path / "first")
    first = register(directory, infrastructure)
    second = register(directory, infrastructure, longitude=-93.255)
    assert first["extraction_artifact_id"] != second["extraction_artifact_id"]
    assert first["extraction"]["raw_sources"] == second["extraction"]["raw_sources"]
    assert len(transport.calls) == 3
    revised_dir, _, _ = bundle(tmp_path / "revision", amount=3.25)
    revised = register(revised_dir, infrastructure)
    assert revised["extraction"]["qpe"]["value"]["value"] == 3.25
    assert (
        revised["extraction"]["raw_sources"][PRODUCTS[0]]
        != first["extraction"]["raw_sources"][PRODUCTS[0]]
    )
    assert (
        revised["extraction"]["raw_sources"][PRODUCTS[1]]
        == first["extraction"]["raw_sources"][PRODUCTS[1]]
    )
    artifacts, factory, _, _ = infrastructure
    assert len(factory.artifacts) == 7  # Four distinct raw sources, three extractions.
    old = replay_registered(ArtifactId(first["extraction_artifact_id"]), artifacts=artifacts)
    assert old["extraction"] == first["extraction"]


def test_raw_checksum_failure_prevents_registration(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    directory, metadata, _ = bundle(tmp_path)
    target = directory / metadata["sources"][PRODUCTS[0]]["filename"]
    target.write_bytes(target.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="checksum"):
        register(directory, infrastructure)
    assert len(infrastructure[1].artifacts) == 0


def test_object_store_checksum_is_verified_on_replay(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    directory, _, _ = bundle(tmp_path)
    result = register(directory, infrastructure)
    artifacts, _, objects, _ = infrastructure
    reference = result["extraction"]["raw_sources"][PRODUCTS[0]]
    objects.objects[reference["content_digest"]] = b"corrupt"
    with pytest.raises(IntegrityError, match="checksum"):
        replay_registered(ArtifactId(result["extraction_artifact_id"]), artifacts=artifacts)


@pytest.mark.parametrize("tampering", ["product_time", "omitted_metadata"])
def test_retained_object_identity_and_complete_metadata_are_required_before_storage(
    tmp_path: Path, infrastructure: tuple[Any, ...], tampering: str
) -> None:
    directory, metadata, _ = bundle(tmp_path)
    if tampering == "omitted_metadata":
        metadata["sources"][PRODUCTS[0]]["parsed_metadata"].pop("temporal")
    else:
        wrong_time = TIME - timedelta(hours=1)
        metadata["product_time"] = wrong_time.isoformat()
        for product in PRODUCTS:
            source = metadata["sources"][product]
            old_path = directory / source["filename"]
            source["url"] = source_url(product, wrong_time)
            source["filename"] = source["url"].rsplit("/", 1)[1]
            old_path.rename(directory / source["filename"])
    (directory / "manifest.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="product time|metadata differs"):
        register(directory, infrastructure)
    assert len(infrastructure[1].artifacts) == 0


@pytest.mark.parametrize(
    "instant",
    [TIME.replace(tzinfo=None), TIME + timedelta(minutes=1), ACQUIRED + timedelta(hours=1)],
)
def test_invalid_requested_time_fails_before_network_or_raw_directory(
    tmp_path: Path, instant: datetime
) -> None:
    transport = Transport({})
    directory = tmp_path / "raw"
    with pytest.raises(ValueError):
        acquire_bundle(
            directory, product_time=instant, transport=transport, clock=FixedClock(ACQUIRED)
        )
    assert transport.calls == []
    assert not directory.exists()


def resolver(raw_root: Path, infrastructure: tuple[Any, ...], **kwargs: Any) -> MRMSHourResolver:
    artifacts, factory, _, snapshot = infrastructure
    return MRMSHourResolver(
        raw_root,
        artifacts=artifacts,
        unit_of_work_factory=factory,
        configuration_snapshot_id=snapshot.configuration_snapshot_id,
        configuration_digest=snapshot.configuration_digest,
        idempotency_lock=kwargs.pop("idempotency_lock", InMemoryIdempotencyLock()),
        clock=kwargs.pop("clock", FixedClock(ACQUIRED)),
        **kwargs,
    )


def resolve(service: MRMSHourResolver, *, longitude: float = -93.265) -> dict[str, Any]:
    return service.resolve_hour(latitude=45.005, longitude=longitude, product_time=TIME)


def test_hour_resolution_reuses_sources_across_coordinates_and_process_instances(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {product: make_mrms_message(product) for product in PRODUCTS}
    transport = Transport(payloads)
    first = resolve(resolver(tmp_path / "cache", infrastructure, transport=transport))
    assert first["acquisition"] == {
        "provider_calls": 3,
        "acquired_bytes": sum(map(len, payloads.values())),
        "raw_reused": False,
        "extraction_reused": False,
        "extraction_bytes": first["byte_size"],
    }

    def no_network() -> Any:
        raise AssertionError("Registered hour reuse must not construct a provider transport")

    monkeypatch.setattr("mesoforge.application.prepared_mrms.default_transport", no_network)
    other_process = resolver(tmp_path / "other-cache", infrastructure)
    second = resolve(other_process, longitude=-93.255)
    repeated = resolve(other_process)
    assert second["extraction"]["raw_sources"] == first["extraction"]["raw_sources"]
    assert second["extraction_artifact_id"] != first["extraction_artifact_id"]
    assert repeated["extraction_artifact_id"] == first["extraction_artifact_id"]
    assert repeated["extraction"] == first["extraction"]
    assert second["acquisition"]["provider_calls"] == repeated["acquisition"]["provider_calls"] == 0
    assert second["acquisition"]["raw_reused"] is True
    assert repeated["acquisition"]["extraction_reused"] is True
    assert len(infrastructure[1].artifacts) == 5  # Three raw sources, two coordinates.
    assert not (tmp_path / "other-cache").exists()


def test_hour_absence_is_retryable_and_completed_raw_products_survive(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    class LaterTransport(Transport):
        absent = True

        def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
            if self.absent and PRODUCTS[1] in url:
                self.calls.append(url)
                return FakeHttpResponse(404, {}, b"not yet")
            return super().get(url, **kwargs)

    payloads = {product: make_mrms_message(product) for product in PRODUCTS}
    transport = LaterTransport(payloads)
    raw_root = tmp_path / "cache"
    with pytest.raises(MRMSUnavailableError, match="404") as failed:
        resolve(resolver(raw_root, infrastructure, transport=transport))
    assert failed.value.provider_calls == 2
    assert failed.value.acquired_bytes == len(payloads[PRODUCTS[0]]) + len(b"not yet")
    assert len(infrastructure[1].artifacts) == 0
    packets = list(raw_root.glob(f"*/{PRODUCTS[0]}/source.json"))
    assert len(packets) == 1
    original = packets[0].read_bytes()

    transport.absent = False
    success = resolve(
        resolver(
            raw_root,
            infrastructure,
            transport=transport,
            clock=FixedClock(ACQUIRED + timedelta(hours=1)),
        )
    )
    assert success["acquisition"]["provider_calls"] == 2
    assert success["acquisition"]["acquired_bytes"] == sum(
        len(payloads[product]) for product in PRODUCTS[1:]
    )
    assert packets[0].read_bytes() == original
    sources = success["extraction"]["source_bundle"]["sources"]
    assert sources[PRODUCTS[0]]["acquired_at"] == ACQUIRED.isoformat()
    assert sources[PRODUCTS[1]]["acquired_at"] == (ACQUIRED + timedelta(hours=1)).isoformat()
    assert len(infrastructure[1].artifacts) == 4


@pytest.mark.parametrize("response_code", [429, 503])
def test_provider_failure_is_not_native_missing(
    tmp_path: Path, infrastructure: tuple[Any, ...], response_code: int
) -> None:
    class FailingTransport(Transport):
        def get(self, url: str, **kwargs: Any) -> FakeHttpResponse:
            self.calls.append(url)
            return FakeHttpResponse(response_code, {}, b"provider failure")

    with pytest.raises(MRMSProviderError, match=str(response_code)) as failed:
        resolve(resolver(tmp_path, infrastructure, transport=FailingTransport({})))
    assert failed.value.provider_calls == 1
    assert failed.value.acquired_bytes == len(b"provider failure")
    assert len(infrastructure[1].artifacts) == 0


def test_malformed_source_preserves_received_bytes_and_never_registers_an_observation(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    payload = b"\x1f\x8bnot a complete gzip/GRIB"
    with pytest.raises(MRMSContractError) as failed:
        resolve(resolver(tmp_path, infrastructure, transport=Transport({PRODUCTS[0]: payload})))
    assert failed.value.provider_calls == 1
    assert failed.value.acquired_bytes == len(payload)
    originals = list(tmp_path.glob("*/*/*.grib2.gz"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == payload
    assert originals[0].with_name("source.json").exists()
    assert len(infrastructure[1].artifacts) == 0


def test_legacy_registered_hour_is_reused_after_advisory_lock_recheck(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _, _ = bundle(tmp_path)
    winner: dict[str, Any] = {}

    class WinnerLock:
        @contextmanager
        def acquire(self, digest: Digest) -> Any:
            winner.update(register(directory, infrastructure))
            key = winner["extraction_artifact_id"]
            # Historical extraction attributes were absent; preserve read compatibility.
            infrastructure[1].artifacts[key] = (
                infrastructure[1].artifacts[key].model_copy(update={"attributes": None})
            )
            yield

    def no_network() -> Any:
        raise AssertionError("The waiting process must recheck the winner's retained hour")

    monkeypatch.setattr("mesoforge.application.prepared_mrms.default_transport", no_network)
    result = resolve(resolver(tmp_path / "cache", infrastructure, idempotency_lock=WinnerLock()))
    assert result["extraction_artifact_id"] == winner["extraction_artifact_id"]
    assert result["acquisition"]["provider_calls"] == 0
    assert result["acquisition"]["extraction_reused"] is True


def test_conflicting_registered_source_revisions_require_explicit_resolution(
    tmp_path: Path, infrastructure: tuple[Any, ...]
) -> None:
    first_dir, _, _ = bundle(tmp_path / "first", amount=2.0)
    revised_dir, _, _ = bundle(tmp_path / "revision", amount=3.0)
    register(first_dir, infrastructure)
    register(revised_dir, infrastructure)
    transport = Transport({})
    with pytest.raises(MRMSContractError, match="Conflicting retained MRMS source revisions"):
        resolve(resolver(tmp_path / "cache", infrastructure, transport=transport))
    assert transport.calls == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_windows_packet_commit_retries_preserve_original_acquisition(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    original_rename = Path.rename
    attempts = []
    staged_records = []
    delays = []

    def temporarily_denied(path: Path, target: Path) -> Path:
        if target.name == PRODUCTS[0]:
            attempts.append((path, target))
            staged_records.append((path / "source.json").read_bytes())
            if len(attempts) <= 2:
                failure = OSError(errno.EACCES, "temporary Windows file sharing violation")
                failure.winerror = winerror
                raise failure
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", temporarily_denied)
    monkeypatch.setattr("mesoforge.application.prepared_mrms.time.sleep", delays.append)
    payloads = {product: make_mrms_message(product) for product in PRODUCTS}
    transport = Transport(payloads)
    result = resolve(resolver(tmp_path, infrastructure, transport=transport))
    assert len(attempts) == 3
    assert delays == [0.05, 0.1]
    assert staged_records[0] == staged_records[1] == staged_records[2]
    packet = attempts[-1][1]
    source = json.loads((packet / "source.json").read_bytes())
    assert source["acquired_at"] == ACQUIRED.isoformat()
    assert (packet / source["filename"]).read_bytes() == payloads[PRODUCTS[0]]
    assert source["content_digest"] == str(Digest.of_bytes(payloads[PRODUCTS[0]]))
    assert result["acquisition"]["provider_calls"] == 3
    assert len(infrastructure[1].artifacts) == 4


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 5), (None, 1)])
def test_persistent_packet_commit_failure_is_bounded_and_retains_unregistered_raw(
    tmp_path: Path,
    infrastructure: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    winerror: int | None,
    expected_attempts: int,
) -> None:
    attempts = []
    delays = []

    def denied(path: Path, target: Path) -> Path:
        attempts.append((path, target))
        failure = OSError(errno.EACCES, "persistent filesystem failure")
        if winerror is not None:
            failure.winerror = winerror
        raise failure

    monkeypatch.setattr(Path, "rename", denied)
    monkeypatch.setattr("mesoforge.application.prepared_mrms.time.sleep", delays.append)
    raw = make_mrms_message(PRODUCTS[0])
    with pytest.raises(MRMSContractError, match="persistent filesystem failure") as failed:
        resolve(resolver(tmp_path, infrastructure, transport=Transport({PRODUCTS[0]: raw})))
    assert len(attempts) == expected_attempts
    assert len(delays) == expected_attempts - 1
    assert failed.value.provider_calls == 1
    assert len(infrastructure[1].artifacts) == 0
    staging, target = attempts[-1]
    assert staging.exists() and not target.exists()
    source = json.loads((staging / "source.json").read_bytes())
    assert source["acquired_at"] == ACQUIRED.isoformat()
    assert (staging / source["filename"]).read_bytes() == raw


def test_packet_commit_never_replaces_a_newly_existing_destination(
    tmp_path: Path, infrastructure: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = []

    def another_destination(path: Path, target: Path) -> Path:
        target.mkdir()
        (target / "preserve").write_bytes(b"original")
        targets.append(target)
        failure = OSError(errno.EACCES, "sharing violation")
        failure.winerror = 5
        raise failure

    monkeypatch.setattr(Path, "rename", another_destination)
    monkeypatch.setattr("mesoforge.application.prepared_mrms.time.sleep", lambda _: None)
    raw = make_mrms_message(PRODUCTS[0])
    with pytest.raises(MRMSContractError, match="Refusing to replace retained MRMS packet"):
        resolve(resolver(tmp_path, infrastructure, transport=Transport({PRODUCTS[0]: raw})))
    assert len(targets) == 1
    assert (targets[0] / "preserve").read_bytes() == b"original"
    assert len(infrastructure[1].artifacts) == 0
