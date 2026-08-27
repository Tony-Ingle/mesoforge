"""Integration test for ConfigurationService against real PostgreSQL
(plan Section 4.7, Task 6 verify command).

Deferred: this test requires the PostgreSQL-backed GridRepository and
ConfigurationRepository built in Task 8
(src/mesoforge/storage/postgres/repositories.py) and the real
UnitOfWork context manager. It is written now as an explicit,
skipped-with-reason placeholder rather than silently omitted, and is
completed (skip removed, real assertions added) in the Task 8 commit
that introduces those repositories.

Plan-required assertions once implemented: atomic grid+snapshot
registration against real PostgreSQL, idempotent identical grid reuse,
rollback when an existing grid ID has a changed definition, and that
registering an existing digest is idempotent and returns the original
snapshot record.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(
    reason=(
        "Requires storage.postgres.repositories (Task 8) and a real "
        "PostgreSQL UnitOfWork; completed alongside Task 8/10."
    )
)
def test_configuration_service_registers_against_real_postgres() -> None:
    raise NotImplementedError
