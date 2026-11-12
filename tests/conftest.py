import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from booxlib.config import Config  # noqa: E402
from tests.fake_boox import FakeBoox, FakeState  # noqa: E402


@pytest.fixture
def fake_boox():
    with FakeBoox() as server:
        yield server


@pytest.fixture
def empty_state():
    return FakeState()


@pytest.fixture
def config(tmp_path):
    """A config pointing at a temporary library and the fake device."""
    return Config(
        raw={
            "sources": {"root": str(tmp_path), "exclude_paths": []},
            "device": {
                "host": "http://127.0.0.1:1",
                "books_root": "/storage/emulated/0/Books",
                "min_free_bytes": 0,
            },
            "naming": {"author_style": "sort", "author_folder_style": "sort", "max_filename_len": 140},
            "formats": {
                "extensions": "epub, pdf, mobi, azw3, djvu",
                "preference": {"epub": 100, "azw3": 90, "mobi": 80, "pdf": 50, "djvu": 40},
                "tiny_epub_bytes": 0,
            },
            "categories": [
                {"name": "History", "keywords": ["history", "empire", "war"]},
                {"name": "Fiction", "keywords": ["novel", "stories"]},
            ],
            "fallback_category": "Unsorted",
            "overrides": {},
            "directory_category": {},
        }
    )
