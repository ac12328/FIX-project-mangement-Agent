"""
Initialize a fresh AgentFIXBPRO database.

Usage:
    python scripts/init_db.py [db_path]
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.connection import get_connection
from src.db.schema import apply_schema, get_table_names


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    if db_path is None:
        from config.settings import DB_PATH
        db_path = str(DB_PATH)

    # Ensure data directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    apply_schema(conn)

    tables = get_table_names(conn)
    print(f"Database initialized at: {db_path}")
    print(f"Tables created: {len(tables)}")
    for t in tables:
        print(f"  - {t}")


if __name__ == "__main__":
    main()
