"""Property tests for configuration digest determinism (Task 6, plan
Section 4.7).

RED: written before src/mesoforge/catalog/configuration.py exists.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from mesoforge.catalog.configuration import compute_configuration_digest, load_configuration_source

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = REPO_ROOT / "configs" / "base.yaml"


@given(indent=st.integers(min_value=1, max_value=6), sort_keys=st.booleans())
@settings(max_examples=10)
def test_digest_is_stable_across_yaml_formatting(indent: int, sort_keys: bool) -> None:
    config, _ = load_configuration_source(base_path=BASE_CONFIG)
    canonical_digest = compute_configuration_digest(config)

    raw = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    reformatted = yaml.dump(raw, indent=indent, sort_keys=sort_keys)

    reparsed_path = BASE_CONFIG.parent / f".reformatted-{indent}-{sort_keys}.yaml"
    reparsed_path.write_text(reformatted, encoding="utf-8")
    try:
        reparsed_config, _ = load_configuration_source(base_path=reparsed_path)
        reparsed_digest = compute_configuration_digest(reparsed_config)
    finally:
        reparsed_path.unlink()

    assert reparsed_digest == canonical_digest
