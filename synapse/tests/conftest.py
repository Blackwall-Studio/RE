import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# isolate brain db per test run
os.environ.setdefault("SYNAPSE_TEST", "1")


@pytest.fixture()
def tmp_brain(tmp_path):
    from synapse.brain import Brain

    brain = Brain(str(tmp_path / "test_brain.db"))
    yield brain
    brain.close()
