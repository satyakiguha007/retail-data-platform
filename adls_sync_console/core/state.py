"""
State management: persist upload history to JSON.
"""
import json
import threading
from pathlib import Path

# Lock for thread-safe writes
_history_lock = threading.Lock()


def get_data_dir() -> Path:
    """Return the data directory (creating it if needed)."""
    data_dir = Path(__file__).resolve().parent.parent / "data"
    data_dir.mkdir(exist_ok=True)
    return data_dir


HISTORY_FILE = get_data_dir() / "history.json"
MAX_HISTORY = 500  # keep last N runs


def load_history() -> list[dict]:
    """Load upload history (most recent first)."""
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def append_history(entry: dict):
    """Append a run entry and trim to MAX_HISTORY."""
    with _history_lock:
        history = load_history()
        history.insert(0, entry)
        history = history[:MAX_HISTORY]
        HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))


def clear_history():
    """Wipe history (useful for testing)."""
    with _history_lock:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
