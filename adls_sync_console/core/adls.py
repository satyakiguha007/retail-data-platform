"""
ADLS client wrapper.

Wraps Azure SDK with conveniences:
  - Loads .env from project root
  - Caches the service client (one per process)
  - Provides a test_connection() method that verifies access to the
    raw container specifically (not account-wide enumeration)
  - Provides upload_file() with retry-style error handling

Note on test_connection: the service principal is scoped to the raw container
only (least privilege). list_file_systems() would require account-level read,
which the SP correctly does NOT have. So we test by reading the raw
container's properties instead — proves auth works and the SP can do its job.
"""
import os
from pathlib import Path
from typing import Optional

from azure.identity import ClientSecretCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv

from .config import PROJECT_ROOT


def _mask(s: Optional[str], visible: int = 4) -> str:
    """Mask a secret: show first 4 chars then asterisks."""
    if not s:
        return ""
    if len(s) <= visible:
        return "*" * len(s)
    return s[:visible] + "*" * (len(s) - visible)


class ADLSClient:
    """Wrapper around Azure SDK for ADLS Gen2 operations."""

    def __init__(self):
        load_dotenv(PROJECT_ROOT / ".env")
        self._service: Optional[DataLakeServiceClient] = None

    # ───────────────────────────── env summary ──────────────────────────────

    def get_env_summary(self) -> dict:
        """Return a dict describing what env vars are present (masked)."""
        required = [
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "AZURE_STORAGE_ACCOUNT",
        ]
        missing = [k for k in required if not os.environ.get(k)]
        return {
            "valid": len(missing) == 0,
            "missing": missing,
            "tenant_id_masked": _mask(os.environ.get("AZURE_TENANT_ID"), 8),
            "client_id_masked": _mask(os.environ.get("AZURE_CLIENT_ID"), 8),
            "client_secret_masked": _mask(os.environ.get("AZURE_CLIENT_SECRET"), 4),
            "storage_account": os.environ.get("AZURE_STORAGE_ACCOUNT"),
        }

    # ──────────────────────────── connection ────────────────────────────────

    def _build_service_client(self) -> DataLakeServiceClient:
        credential = ClientSecretCredential(
            tenant_id=os.environ["AZURE_TENANT_ID"],
            client_id=os.environ["AZURE_CLIENT_ID"],
            client_secret=os.environ["AZURE_CLIENT_SECRET"],
        )
        account = os.environ["AZURE_STORAGE_ACCOUNT"]
        return DataLakeServiceClient(
            account_url=f"https://{account}.dfs.core.windows.net",
            credential=credential,
        )

    def test_connection(self) -> tuple[bool, str, dict]:
        """
        Verify the SP can access its target container (raw).

        Does NOT call list_file_systems() because that requires account-level
        read permission, which the SP intentionally does not have under our
        least-privilege design. Instead, reads properties of the raw
        container — proves auth works AND the SP has the right scope.
        """
        env = self.get_env_summary()
        if not env["valid"]:
            return False, f"Missing env vars: {', '.join(env['missing'])}", {}

        raw_container = os.environ.get("AZURE_STORAGE_CONTAINER_RAW", "raw")

        try:
            self._service = self._build_service_client()
            fs = self._service.get_file_system_client(raw_container)

            # Read container properties — requires only container-level access
            props = fs.get_file_system_properties()

            details = {
                "container_count": 1,
                "containers": [raw_container],
                "storage_account": env["storage_account"],
                "region": "Central India",
                "scope_note": f"Service principal scoped to '{raw_container}' (least privilege)",
                "container_last_modified": (
                    props.last_modified.isoformat() if props.last_modified else None
                ),
            }
            return (
                True,
                f"Connected. Service principal can access the '{raw_container}' container.",
                details,
            )
        except Exception as e:
            err_msg = str(e)[:300]
            err_type = type(e).__name__

            # Provide a specific hint for the common permission-mismatch case
            if "AuthorizationPermissionMismatch" in err_msg or "AuthorizationFailed" in err_msg:
                hint = (
                    f"The service principal may not have 'Storage Blob Data Contributor' "
                    f"on the '{raw_container}' container. Check Azure Portal → "
                    f"Storage account → Access Control (IAM) → Role assignments."
                )
                return False, f"{err_type}: {err_msg}\n\n→ {hint}", {}

            return False, f"{err_type}: {err_msg}", {}

    @property
    def service(self) -> DataLakeServiceClient:
        if self._service is None:
            self._service = self._build_service_client()
        return self._service

    # ──────────────────────────── upload ─────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        container: str,
        remote_path: str,
        overwrite: bool = True,
    ) -> tuple[bool, Optional[str]]:
        """Upload a single file. Returns (success, error_message)."""
        try:
            fs = self.service.get_file_system_client(container)
            with open(local_path, "rb") as f:
                fs.get_file_client(remote_path).upload_data(f.read(), overwrite=overwrite)
            return True, None
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:200]}"

    # ───────────────────────── remote listing ────────────────────────────────

    def list_remote_files(self, container: str, prefix: str) -> list[str]:
        """List all files under container/prefix/. Returns list of remote paths."""
        try:
            fs = self.service.get_file_system_client(container)
            paths = []
            for p in fs.get_paths(path=prefix, recursive=True):
                if not p.is_directory:
                    paths.append(p.name)
            return paths
        except Exception:
            return []

    def count_remote_files(self, container: str, prefix: str) -> int:
        """Count files under container/prefix/."""
        return len(self.list_remote_files(container, prefix))