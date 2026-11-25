import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    def load(name: str):
        text = (FIXTURES / name).read_text()
        return json.loads(text) if name.endswith(".json") else text

    return load
