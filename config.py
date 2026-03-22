import os
from pathlib import Path
from dotenv import load_dotenv

def load_env():
    """Load .env file from backend/ directory. Call this once at startup."""
    root_dir = Path(__file__).resolve().parents[0]  # .../apa-kernel/
    env_path = root_dir / ".env"
    load_dotenv(env_path)

def get_env(key: str, default: str | None = None) -> str:
    """Get environment variable."""
    value = os.getenv(key, default)
    if value is None:
        raise ValueError(f"Environment variable {key} not set")
    return value

# To Auto-load environment on import
load_env()

# AZURE FOUNDRY models = (o4 mini, gpt-4.1, text-embedding-3-small)
AZURE_FOUNDRY_ENDPOINT : str = get_env("AZURE_FOUNDRY_ENDPOINT")
AZURE_FOUNDRY_API_VERSION : str = get_env("AZURE_FOUNDRY_API_VERSION") 
AZURE_FOUNDRY_API_KEY : str = get_env("AZURE_FOUNDRY_API_KEY")

