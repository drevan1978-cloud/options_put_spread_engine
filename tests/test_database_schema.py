from __future__ import annotations

import sqlite3
from pathlib import Path

from options_engine.storage.database import REQUIRED_TABLES, initialize_database, list_table_names
from options_engine.storage.models import model_table_names


def test_database_initializes_with_all_required_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"

    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        table_names = list_table_names(connection)

    assert set(REQUIRED_TABLES).issubset(table_names)


def test_all_storage_models_map_to_required_tables() -> None:
    mapped_tables = set(model_table_names().values())

    assert mapped_tables == set(REQUIRED_TABLES)


def test_required_tables_have_created_at_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "engine.sqlite"
    initialize_database(database_path)

    with sqlite3.connect(database_path) as connection:
        missing_created_at = []
        for table_name in REQUIRED_TABLES:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}
            if "created_at" not in columns:
                missing_created_at.append(table_name)

    assert missing_created_at == []
