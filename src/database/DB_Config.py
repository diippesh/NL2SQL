import sqlite3
from typing import Optional, Dict, Any, List
import psycopg2
from psycopg2 import OperationalError, sql
import pandas as pd
import logging
from contextlib import contextmanager
from abc import ABC, abstractmethod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@contextmanager
def get_connection(
    db_name: str,
    db_type: str,
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
):
    """Context manager that opens a database connection and always closes it."""
    conn = None
    try:
        if db_type.lower() == "postgresql":
            conn = psycopg2.connect(
                dbname=db_name,
                user=user or "",
                password=password or "",
                host=host or "localhost",
            )
            logger.info("Connected to PostgreSQL database.")
        elif db_type.lower() == "sqlite":
            conn = sqlite3.connect(db_name)
            logger.info("Connected to SQLite database.")
        else:
            logger.error(f"Unsupported database type: {db_type}")
            yield None
            return
        yield conn
    except OperationalError as e:
        logger.error(f"Operational error while connecting to the database: {e}")
        yield None
    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed.")


def query_database(
    query: str,
    db_name: str,
    db_type: str,
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> pd.DataFrame:
    """
    Execute a SQL query and return the results as a DataFrame.
    Raises on failure (connection error, bad SQL, missing table, etc.) so the
    caller can show the real error instead of silently returning an empty result.
    """
    with get_connection(db_name, db_type, host, user, password) as conn:
        if conn is None:
            raise ConnectionError("Could not connect to the database.")
        df = pd.read_sql_query(query, conn)
        logger.info(f"Query executed successfully, {len(df)} rows returned.")
        return df


# --- Schema Extraction ---
# Kept intentionally lean: columns, primary/foreign keys, and a few sample
# rows is enough context for the LLM to write accurate SQL, without the
# extra index/trigger metadata the original version also collected.

class SchemaExtractor(ABC):
    def __init__(self, connection):
        self.conn = connection

    @abstractmethod
    def get_tables(self) -> List[str]:
        ...

    @abstractmethod
    def get_table_info(self, table_name: str) -> Dict[str, Any]:
        ...


class SQLiteSchemaExtractor(SchemaExtractor):
    def get_tables(self) -> List[str]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_table_info(self, table_name: str) -> Dict[str, Any]:
        cursor = self.conn.cursor()

        cursor.execute(f"PRAGMA table_info('{table_name}');")
        columns = {}
        primary_keys = []
        for col_id, col_name, col_type, not_null, default_val, pk in cursor.fetchall():
            columns[col_name] = {
                "type": col_type,
                "nullable": not not_null,
                "primary_key": bool(pk),
            }
            if pk:
                primary_keys.append(col_name)

        cursor.execute(f"PRAGMA foreign_key_list('{table_name}');")
        foreign_keys = [
            {"from_column": from_col, "to_table": ref_table, "to_column": to_col}
            for (_, _, ref_table, from_col, to_col, *_rest) in cursor.fetchall()
        ]

        sample_data = []
        try:
            cursor.execute(f"SELECT * FROM '{table_name}' LIMIT 3;")
            rows = cursor.fetchall()
            if rows:
                col_names = [desc[0] for desc in cursor.description]
                sample_data = [dict(zip(col_names, row)) for row in rows]
        except Exception as e:
            logger.warning(f"Unable to retrieve sample data for table {table_name}: {e}")

        return {
            "columns": columns,
            "primary_keys": primary_keys,
            "foreign_keys": foreign_keys,
            "sample_data": sample_data,
        }


class PostgreSQLSchemaExtractor(SchemaExtractor):
    def get_tables(self) -> List[str]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
        )
        return [row[0] for row in cursor.fetchall()]

    def get_table_info(self, table_name: str) -> Dict[str, Any]:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = %s AND table_schema = 'public';
            """,
            [table_name],
        )
        columns = {}
        for col_name, data_type, is_nullable in cursor.fetchall():
            columns[col_name] = {
                "type": data_type,
                "nullable": is_nullable.upper() == "YES",
                "primary_key": False,
            }

        cursor.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public';
            """,
            [table_name],
        )
        primary_keys = [row[0] for row in cursor.fetchall()]
        for pk_col in primary_keys:
            if pk_col in columns:
                columns[pk_col]["primary_key"] = True

        cursor.execute(
            """
            SELECT kcu.column_name, ccu.table_name, ccu.column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
              ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = %s AND tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public';
            """,
            [table_name],
        )
        foreign_keys = [
            {"from_column": from_col, "to_table": to_table, "to_column": to_col}
            for from_col, to_table, to_col in cursor.fetchall()
        ]

        sample_data = []
        try:
            cursor.execute(
                sql.SQL("SELECT * FROM {} LIMIT 3;").format(sql.Identifier(table_name))
            )
            rows = cursor.fetchall()
            if rows:
                col_names = [desc[0] for desc in cursor.description]
                sample_data = [dict(zip(col_names, row)) for row in rows]
        except Exception as e:
            logger.warning(f"Unable to retrieve sample data for table {table_name}: {e}")

        return {
            "columns": columns,
            "primary_keys": primary_keys,
            "foreign_keys": foreign_keys,
            "sample_data": sample_data,
        }


def get_all_schemas(
    db_name: str,
    db_type: str,
    host: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return {table_name: {columns, primary_keys, foreign_keys, sample_data}} for every table."""
    schemas: Dict[str, Dict[str, Any]] = {}

    with get_connection(db_name, db_type, host, user, password) as conn:
        if not conn:
            logger.error("Database connection failed. Returning empty schema.")
            return {}

        if db_type.lower() == "sqlite":
            extractor = SQLiteSchemaExtractor(conn)
        elif db_type.lower() == "postgresql":
            extractor = PostgreSQLSchemaExtractor(conn)
        else:
            logger.error(f"Unsupported database type: {db_type}")
            return {}

        for table in extractor.get_tables():
            schemas[table] = extractor.get_table_info(table)

    return schemas
