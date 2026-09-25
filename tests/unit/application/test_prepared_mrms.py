"""Bounded MRMS acquisition, immutable artifact lineage and read-only offline replay."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mesoforge.application.artifacts import ArtifactService
from mesoforge.application.configuration import ConfigurationService
from mesoforge.application.prepared_mrms import (
    PRODUCTS,
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
        product = url.split("/")[-2]
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
