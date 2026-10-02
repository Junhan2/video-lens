"""video-lens: local video analysis. Package root holds the version constants and skill paths."""
from pathlib import Path

TOOL_VERSION = "1.1.1"
SCHEMA = "video-lens/1"

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPTS_DIR.parent
SWIFT_DIR = SKILL_DIR / "swift"
REFERENCE_DIR = SKILL_DIR / "reference"
SELFTEST_DIR = SKILL_DIR / "selftest"
