import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STL_DIR", tempfile.mkdtemp(prefix="stl-mcp-tests-"))

import pytest

import server
from store import Store


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Point the server (standalone files AND the model library) at a fresh, empty directory."""
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "OUT", tmp_path)
    monkeypatch.setattr(server, "store", Store(tmp_path, allow_script=False))
    return tmp_path
