"""
Sync engine: upload all files for a source from local to ADLS.

Uses ThreadPoolExecutor for parallelism. Calls a progress callback after each
file so the UI can update.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from .adls import ADLSClient
from .config import resolve_local_root


def sync_source(
    client: ADLSClient,
    source: dict,
    on_progress: Optional[Callable[[int, int, str, bool], None]] = None,
    max_workers: int = 8,
) -> dict:
    """
    Sync all files for a source to ADLS.

    Args:
      client: connected ADLSClient
      source: source config dict
      on_progress: callable(done, total, current_file, success) — called per file
      max_workers: thread count for parallel uploads

    Returns:
      result dict with: total, uploaded, failed, errors, elapsed_seconds
    """
    local_root = resolve_local_root(source)
    if not local_root.exists():
        return {
            "total": 0,
            "uploaded": 0,
            "failed": 0,
            "errors": [{"file": "(none)", "error": f"Local path not found: {local_root}"}],
            "elapsed_seconds": 0.0,
        }

    # Find all files
    files = [f for f in local_root.rglob("*") if f.is_file()]

    container = source["remote_container"]
    prefix = source["remote_prefix"]

    def upload_one(local_file: Path) -> tuple[Path, bool, Optional[str]]:
        rel = local_file.relative_to(local_root)
        remote_path = f"{prefix}/{rel.as_posix()}"
        success, error = client.upload_file(local_file, container, remote_path)
        return local_file, success, error

    start = time.time()
    uploaded = 0
    failed = 0
    errors = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(upload_one, f): f for f in files}
        done = 0
        for future in as_completed(futures):
            local_file, success, error = future.result()
            done += 1
            if success:
                uploaded += 1
            else:
                failed += 1
                errors.append({"file": str(local_file.relative_to(local_root)), "error": error})
            if on_progress:
                on_progress(done, len(files), str(local_file.relative_to(local_root)), success)

    elapsed = time.time() - start

    return {
        "total": len(files),
        "uploaded": uploaded,
        "failed": failed,
        "errors": errors,
        "elapsed_seconds": elapsed,
    }
