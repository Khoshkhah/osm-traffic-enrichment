"""
motherduck_sync.py — Sync local osm-traffic-enrichment DuckDB files to MotherDuck cloud.

USAGE:
    python scripts/motherduck_sync.py sodermalm           # auto-detect full vs incremental
    python scripts/motherduck_sync.py sodermalm --full    # force full migration
    python scripts/motherduck_sync.py --all               # sync all known databases

AUTHENTICATION:
    Set MOTHERDUCK_TOKEN in .env or environment:
        export MOTHERDUCK_TOKEN=your_token_here

SYNC MODES:
    full        : Copy the entire local DuckDB to MotherDuck (all schemas/tables).
                  Runs automatically on first sync when the remote DB has no tables.
                  Re-running overwrites existing remote tables.
    incremental : Push only new rows since the last sync.
                  - Traffic tables (main.runs, main.*_congestion_history): watermarked by run_id.
                  - Selection tables (main.saved_selections*): watermarked by selection_id.
                  Used automatically when the remote DB already contains data.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import duckdb
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

log = logging.getLogger(__name__)

DB_DIR = Path(__file__).parent.parent / "db"
KNOWN_DBS = ["sodermalm", "sundbyberg", "tartu", "nacka", "lidingo"]

# Tables synced incrementally using run_id as watermark
_TRAFFIC_TABLES = [
    ("main", "runs"),
    ("main", "mapbox_congestion_history"),
    ("main", "google_congestion_history"),
    ("main", "tomtom_congestion_history"),
]

# Tables synced incrementally using selection_id as watermark
_SELECTION_TABLES = [
    ("main", "saved_selections_meta"),
    ("main", "saved_selections"),
]


# ── Connection ────────────────────────────────────────────────────────────────


def _get_token() -> str:
    token = os.environ.get("MOTHERDUCK_TOKEN", "")
    if not token:
        raise ValueError(
            "MOTHERDUCK_TOKEN not set. "
            "Add it to .env or run: export MOTHERDUCK_TOKEN=your_token"
        )
    return token


def _md_connect_root() -> duckdb.DuckDBPyConnection:
    """Connect to the MotherDuck root session (not database-specific)."""
    token = _get_token()
    os.environ["motherduck_token"] = token
    return duckdb.connect(f"md:?motherduck_token={token}")


def _ensure_remote_db(db_name: str) -> None:
    """Create the MotherDuck database if it doesn't already exist."""
    con = _md_connect_root()
    try:
        con.execute(f'CREATE DATABASE IF NOT EXISTS "{db_name}"')
    finally:
        con.close()


def _md_connect(db_name: str) -> duckdb.DuckDBPyConnection:
    """Open a MotherDuck connection for the given database, creating it if needed."""
    token = _get_token()
    os.environ["motherduck_token"] = token
    _ensure_remote_db(db_name)
    return duckdb.connect(f"md:{db_name}?motherduck_token={token}")


def _table_exists(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()[0] > 0


def _remote_has_tables(con: duckdb.DuckDBPyConnection) -> bool:
    """Return True if the MotherDuck database already contains any user tables."""
    return con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')"
    ).fetchone()[0] > 0


# ── Full sync ─────────────────────────────────────────────────────────────────


def sync_full(local_db_path: Path) -> None:
    """
    Copy every schema and table from the local DuckDB to MotherDuck.
    Re-running is safe — existing remote tables are overwritten.
    """
    db_name = local_db_path.stem
    log.info(f"  Full sync: {local_db_path.name} → md:{db_name}")

    con = _md_connect(db_name)
    try:
        local_path = str(local_db_path.resolve())
        con.execute(f"ATTACH '{local_path}' AS local_db (READ_ONLY)")
        try:
            tables = con.execute("""
                SELECT schema_name, table_name
                FROM duckdb_tables()
                WHERE database_name = 'local_db'
                  AND internal = false
                ORDER BY schema_name, table_name
            """).fetchall()

            log.info(f"  Copying {len(tables)} tables...")
            for schema, table in tables:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                con.execute(
                    f'CREATE OR REPLACE TABLE "{schema}"."{table}" AS '
                    f'SELECT * FROM local_db."{schema}"."{table}"'
                )
                log.debug(f"    {schema}.{table}")

            log.info("  Full sync complete.")
        finally:
            con.execute("DETACH local_db")
    finally:
        con.close()


# ── Incremental sync ──────────────────────────────────────────────────────────


def sync_incremental(local_db_path: Path) -> None:
    """
    Push only rows added since the last sync.
    - Traffic tables: uses run_id watermark.
    - Selection tables: uses selection_id NOT IN.
    """
    db_name = local_db_path.stem
    log.info(f"  Incremental sync: {local_db_path.name} → md:{db_name}")

    con = _md_connect(db_name)
    local_path = str(local_db_path.resolve())
    try:
        con.execute(f"ATTACH '{local_path}' AS local_db (READ_ONLY)")
        try:
            _sync_traffic(con)
            _sync_selections(con)
        finally:
            con.execute("DETACH local_db")
    finally:
        con.close()


def _sync_traffic(con: duckdb.DuckDBPyConnection) -> None:
    """Insert traffic rows with run_id greater than the remote maximum."""
    max_run_id = con.execute(
        "SELECT COALESCE(MAX(run_id), 0) FROM main.runs"
    ).fetchone()[0]

    new_count = con.execute(
        "SELECT count(*) FROM local_db.main.runs WHERE run_id > ?",
        [max_run_id],
    ).fetchone()[0]

    if new_count == 0:
        log.info("  No new traffic runs to sync.")
        return

    log.info(f"  Syncing {new_count} new run(s) (run_id > {max_run_id})...")
    con.begin()
    try:
        for schema, table in _TRAFFIC_TABLES:
            local_exists = con.execute(
                "SELECT count(*) FROM duckdb_tables() "
                "WHERE database_name = 'local_db' AND schema_name = ? AND table_name = ?",
                [schema, table],
            ).fetchone()[0]
            if not local_exists:
                continue

            if not _table_exists(con, schema, table):
                con.execute(
                    f'CREATE TABLE "{schema}"."{table}" AS '
                    f'SELECT * FROM local_db."{schema}"."{table}" WHERE run_id > ?',
                    [max_run_id],
                )
            else:
                con.execute(
                    f'INSERT INTO "{schema}"."{table}" '
                    f'SELECT * FROM local_db."{schema}"."{table}" WHERE run_id > ?',
                    [max_run_id],
                )
        con.commit()
        log.info("  Traffic tables synced.")
    except Exception:
        con.rollback()
        raise


def _sync_selections(con: duckdb.DuckDBPyConnection) -> None:
    """Upsert edge selections using selection_id as the uniqueness key."""
    for schema, table in _SELECTION_TABLES:
        local_exists = con.execute(
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = 'local_db' AND schema_name = ? AND table_name = ?",
            [schema, table],
        ).fetchone()[0]
        if not local_exists:
            continue

        if not _table_exists(con, schema, table):
            con.execute(
                f'CREATE TABLE "{schema}"."{table}" AS '
                f'SELECT * FROM local_db."{schema}"."{table}"'
            )
            log.info(f"  Created remote {schema}.{table}")
        else:
            con.execute(
                f'INSERT INTO "{schema}"."{table}" '
                f'SELECT * FROM local_db."{schema}"."{table}" l '
                f'WHERE l.selection_id NOT IN '
                f'(SELECT selection_id FROM "{schema}"."{table}")'
            )
            log.debug(f"  {schema}.{table} selections checked.")


# ── Main entry point ──────────────────────────────────────────────────────────


def sync(local_db_path: Path, force_full: bool = False) -> None:
    """
    Auto-detect full vs incremental sync and run the appropriate mode.
    Called programmatically from pipeline_traffic.py.
    """
    if not local_db_path.exists():
        raise FileNotFoundError(f"Local DuckDB not found: {local_db_path}")

    con = _md_connect(local_db_path.stem)
    try:
        remote_populated = _remote_has_tables(con)
    finally:
        con.close()

    if force_full or not remote_populated:
        sync_full(local_db_path)
    else:
        sync_incremental(local_db_path)


# ── CLI ───────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(
        description="Sync local DuckDB files to MotherDuck cloud.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Database name to sync (e.g. 'sodermalm'). Omit when using --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Sync all known databases: {', '.join(KNOWN_DBS)}",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full migration even if the remote database already exists.",
    )
    parser.add_argument(
        "--db-dir",
        default=str(DB_DIR),
        help=f"Directory containing .duckdb files (default: {DB_DIR})",
    )
    args = parser.parse_args()

    if not args.name and not args.all:
        parser.error("Provide a database name or use --all")

    db_dir = Path(args.db_dir)
    names = KNOWN_DBS if args.all else [args.name]

    failed = []
    for name in names:
        db_path = db_dir / f"{name}.duckdb"
        log.info(f"\n{'='*50}")
        log.info(f"  {name}")
        log.info(f"{'='*50}")
        try:
            sync(db_path, force_full=args.full)
        except Exception as exc:
            log.error(f"  ERROR: {exc}")
            failed.append(name)

    if failed:
        sys.exit(f"\nSync failed for: {', '.join(failed)}")
