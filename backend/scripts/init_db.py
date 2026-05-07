import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402


def main() -> int:
    Base.metadata.create_all(bind=engine)
    print("database schema initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

