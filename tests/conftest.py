import sys
from pathlib import Path

# Allow `from custom_components.wallbox_ble import bapi` in tests without
# installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
