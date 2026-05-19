"""Quick smoke test: upload a file to ADLS using the service principal."""
import os
from datetime import datetime
from azure.identity import ClientSecretCredential
from azure.storage.filedatalake import DataLakeServiceClient
from dotenv import load_dotenv

load_dotenv()

# Build credential from .env
credential = ClientSecretCredential(
    tenant_id=os.environ["AZURE_TENANT_ID"],
    client_id=os.environ["AZURE_CLIENT_ID"],
    client_secret=os.environ["AZURE_CLIENT_SECRET"],
)

# Connect to ADLS
account = os.environ["AZURE_STORAGE_ACCOUNT"]
service = DataLakeServiceClient(
    account_url=f"https://{account}.dfs.core.windows.net",
    credential=credential,
)

# Get the raw container
fs = service.get_file_system_client(file_system="raw")

# Upload a test file to raw/_smoke_test/
path = f"_smoke_test/test_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
content = b"Hello from laptop. If you see this in Portal, ADLS write works."

file_client = fs.get_file_client(path)
file_client.upload_data(content, overwrite=True)

print(f"✓ Uploaded to: abfss://raw@{account}.dfs.core.windows.net/{path}")
print(f"  Size: {len(content)} bytes")