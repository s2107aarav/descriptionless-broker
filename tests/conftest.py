import pytest
from backend.store import AuditStore
from evals.poison_registry import get_clean_registry


@pytest.fixture
def runtime(tmp_path):
    registry = get_clean_registry()
    store = AuditStore(tmp_path / "isolated-ledger.db", capture_prompts=True)
    yield {"registry": registry, "store": store}
    registry.close()
