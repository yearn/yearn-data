"""Vercel imports the handler; importing this module never starts a server."""
from pathlib import Path
import sys

# The repository uses a src layout; no editable installation is needed at runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from yearn_data.hosted_api import Handler


class handler(Handler):
    """File-based Vercel function entry point."""
