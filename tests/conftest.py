import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STL_DIR", tempfile.mkdtemp(prefix="stl-mcp-tests-"))

import pytest

import server


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Point the server at a fresh, empty output directory."""
    monkeypatch.setattr(server, "OUT", tmp_path)
    return tmp_path
