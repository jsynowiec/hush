from pathlib import Path

from hush import sanitize

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Real captured tool output, cleaned the way hush cleans it."""
    raw = (FIXTURES / f"{name}.txt").read_text()
    return sanitize.normalize(raw).replace("/work/", "")
