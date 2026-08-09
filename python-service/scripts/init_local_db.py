from app.config import get_settings
from app.sqlite_store import SQLiteStore


def main() -> None:
    store = SQLiteStore(get_settings())
    print(f"SQLite initialized at {store.path}")
    print(store.counts())


if __name__ == "__main__":
    main()
