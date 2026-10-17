"""Installer guard. Exit 0 when this user's FORGE service is responding."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from launcher import alive, data_directory
sys.exit(0 if alive(data_directory()) else 1)
