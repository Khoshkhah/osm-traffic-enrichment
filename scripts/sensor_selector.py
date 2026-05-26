#!/usr/bin/env python3
"""
sensor_selector.py — Interactive road segment selection backend.

Starts a lightweight HTTP server on port 8080 and opens a web browser to
allow visual selection of road segments for placing traffic sensors.
"""

import http.server
import json
import logging
import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import duckdb

# Setup logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("sensor_selector")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "db"
HTML_FILE = BASE_DIR / "scripts" / "sensor_selector.html"


class SensorSelectorHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress verbose standard HTTP requests logs to keep console clean
        log.debug(format % args)

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        # ── Route 1: Base Dashboard HTML ──────────────────────────────────────
        if path == "/" or path == "/index.html":
            self.serve_html()

        # ── Route 2: List Available Databases ────────────────────────────────
        elif path == "/api/databases":
            self.serve_databases()

        # ── Route 3: Query and Fetch Roads GeoJSON ────────────────────────────
        elif path == "/api/roads":
            db_name = query.get("db", [None])[0]
            mapbox_run = query.get("mapbox_run", [None])[0]
            google_run = query.get("google_run", [None])[0]
            tomtom_run = query.get("tomtom_run", [None])[0]
            self.serve_roads(db_name, mapbox_run, google_run, tomtom_run)

        # ── Route 4: Query and Fetch H3 Boundary Cells GeoJSON ────────────────
        elif path == "/api/h3":
            db_name = query.get("db", [None])[0]
            self.serve_h3_cells(db_name)

        # ── Route 5: Query and Fetch Area Boundary GeoJSON ────────────────────
        elif path == "/api/boundary":
            db_name = query.get("db", [None])[0]
            self.serve_boundary(db_name)

        # ── Route 6: Query and Fetch Historical Runs/Snapshots ────────────────
        elif path == "/api/runs":
            db_name = query.get("db", [None])[0]
            self.serve_runs(db_name)

        # ── Route 7: List Saved Selections ────────────────────────────────────
        elif path == "/api/selections":
            db_name = query.get("db", [None])[0]
            self.serve_selections(db_name)

        # ── Route 8: Fetch Saved Selection Edges as GeoJSON ───────────────────
        elif path == "/api/selections/edges":
            db_name = query.get("db", [None])[0]
            selection_id = query.get("selection_id", [None])[0]
            self.serve_selection_edges(db_name, selection_id)

        else:
            self.send_error(404, "File Not Found")

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/api/selections":
            db_name = query.get("db", [None])[0]
            if not db_name:
                self.send_error(400, "Missing 'db' query parameter")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode("utf-8"))
                self.save_selection(db_name, data)
            except Exception as e:
                log.error(f"Failed to parse POST body: {e}")
                self.send_error(400, f"Invalid JSON or request: {e}")
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/api/selections":
            db_name = query.get("db", [None])[0]
            selection_id = query.get("selection_id", [None])[0]
            if not db_name or not selection_id:
                self.send_error(400, "Missing 'db' or 'selection_id' parameter")
                return
            self.delete_selection(db_name, selection_id)
        else:
            self.send_error(404, "Not Found")

    def serve_html(self):
        """Serve the selector html page."""
        if not HTML_FILE.exists():
            self.send_error(500, f"HTML file not found: {HTML_FILE}")
            return
        
        try:
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        except Exception as e:
            self.send_error(500, f"Error reading dashboard: {e}")

    def serve_databases(self):
        """Scan db/ directory and return list of available databases."""
        if not DB_DIR.exists():
            self.send_json([])
            return

        db_files = [f.name for f in DB_DIR.glob("*.duckdb") if f.is_file()]
        db_files.sort()
        self.send_json(db_files)

    def serve_roads(self, db_name, mapbox_run=None, google_run=None, tomtom_run=None):
        """Connect to selected DuckDB and return roads as GeoJSON FeatureCollection."""
        if not db_name:
            self.send_error(400, "Missing 'db' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        log.info(f"Querying road network from database: {db_name}")
        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            con.execute("LOAD spatial")
            
            # Check which tables exist in main schema
            existing_tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
            
            ctes = []
            joins = []
            selects = []
            
            if "mapbox_congestion_history" in existing_tables:
                if mapbox_run and mapbox_run.lower() != "latest":
                    try:
                        mapbox_run_id = int(mapbox_run)
                        ctes.append(f"""
                            latest_mapbox AS (
                                SELECT edge_id, congestion
                                FROM mapbox_congestion_history
                                WHERE run_id = {mapbox_run_id}
                            )
                        """)
                    except ValueError:
                        ctes.append("""
                            latest_mapbox AS (
                                SELECT DISTINCT ON (edge_id) edge_id, congestion
                                FROM mapbox_congestion_history
                                ORDER BY edge_id, matched_at DESC
                            )
                        """)
                else:
                    ctes.append("""
                        latest_mapbox AS (
                            SELECT DISTINCT ON (edge_id) edge_id, congestion
                            FROM mapbox_congestion_history
                            ORDER BY edge_id, matched_at DESC
                        )
                    """)
                selects.append("coalesce(m.congestion, 'no data') AS mb_congestion")
                joins.append("LEFT JOIN latest_mapbox m ON e.edge_id = m.edge_id")
            else:
                selects.append("'no data' AS mb_congestion")
                
            if "google_congestion_history" in existing_tables:
                if google_run and google_run.lower() != "latest":
                    try:
                        google_run_id = int(google_run)
                        ctes.append(f"""
                            latest_google AS (
                                SELECT edge_id, congestion
                                FROM google_congestion_history
                                WHERE run_id = {google_run_id}
                            )
                        """)
                    except ValueError:
                        ctes.append("""
                            latest_google AS (
                                SELECT DISTINCT ON (edge_id) edge_id, congestion
                                FROM google_congestion_history
                                ORDER BY edge_id, matched_at DESC
                            )
                        """)
                else:
                    ctes.append("""
                        latest_google AS (
                            SELECT DISTINCT ON (edge_id) edge_id, congestion
                            FROM google_congestion_history
                            ORDER BY edge_id, matched_at DESC
                        )
                    """)
                selects.append("coalesce(g.congestion, 'no data') AS gg_congestion")
                joins.append("LEFT JOIN latest_google g ON e.edge_id = g.edge_id")
            else:
                selects.append("'no data' AS gg_congestion")
                
            if "tomtom_congestion_history" in existing_tables:
                if tomtom_run and tomtom_run.lower() != "latest":
                    try:
                        tomtom_run_id = int(tomtom_run)
                        ctes.append(f"""
                            latest_tomtom AS (
                                SELECT edge_id, congestion
                                FROM tomtom_congestion_history
                                WHERE run_id = {tomtom_run_id}
                            )
                        """)
                    except ValueError:
                        ctes.append("""
                            latest_tomtom AS (
                                SELECT DISTINCT ON (edge_id) edge_id, congestion
                                FROM tomtom_congestion_history
                                ORDER BY edge_id, matched_at DESC
                            )
                        """)
                else:
                    ctes.append("""
                        latest_tomtom AS (
                            SELECT DISTINCT ON (edge_id) edge_id, congestion
                            FROM tomtom_congestion_history
                            ORDER BY edge_id, matched_at DESC
                        )
                    """)
                selects.append("coalesce(t.congestion, 'no data') AS tt_congestion")
                joins.append("LEFT JOIN latest_tomtom t ON e.edge_id = t.edge_id")
            else:
                selects.append("'no data' AS tt_congestion")

            cte_block = f"WITH {', '.join(ctes)}" if ctes else ""
            
            query = f"""
                {cte_block}
                SELECT 
                    e.edge_id, 
                    coalesce(e.name, 'Unnamed Road') AS name, 
                    e.highway, 
                    e.length_m,
                    {', '.join(selects)},
                    ST_AsGeoJSON(e.geometry) AS geojson
                FROM driving.edges e
                { ' '.join(joins) }
            """
            
            rows = con.execute(query).fetchall()
            
            features = []
            for edge_id, name, highway, length_m, mb_cong, gg_cong, tt_cong, geojson_str in rows:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geojson_str),
                    "properties": {
                        "edge_id": edge_id,
                        "name": name,
                        "highway": highway,
                        "length_m": round(length_m, 1),
                        "mb_congestion": mb_cong,
                        "gg_congestion": gg_cong,
                        "tt_congestion": tt_cong
                    }
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features
            }
            self.send_json(geojson)
        except Exception as e:
            log.error(f"Error querying roads from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def serve_h3_cells(self, db_name):
        """Connect to selected DuckDB and return H3 cells as GeoJSON FeatureCollection."""
        if not db_name:
            self.send_error(400, "Missing 'db' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        log.info(f"Querying H3 grid cells from database: {db_name}")
        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            con.execute("LOAD spatial")
            
            # Check if boundary_cells table exists
            tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_name = 'boundary_cells'").fetchall()
            if not tables:
                # Table doesn't exist, return empty collection
                self.send_json({"type": "FeatureCollection", "features": []})
                return

            query = """
                SELECT 
                    h3_id, 
                    resolution, 
                    ST_AsGeoJSON(geometry) AS geojson 
                FROM boundary_cells
            """
            rows = con.execute(query).fetchall()
            
            features = []
            for h3_id, resolution, geojson_str in rows:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geojson_str),
                    "properties": {
                        "h3_id": h3_id,
                        "resolution": resolution
                    }
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features
            }
            self.send_json(geojson)
        except Exception as e:
            log.error(f"Error querying H3 cells from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def serve_boundary(self, db_name):
        """Connect to selected DuckDB and return area boundary as GeoJSON FeatureCollection."""
        if not db_name:
            self.send_error(400, "Missing 'db' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        log.info(f"Querying boundary from database: {db_name}")
        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            con.execute("LOAD spatial")
            
            # Check if boundary table exists
            tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_name = 'boundary'").fetchall()
            if not tables:
                self.send_json({"type": "FeatureCollection", "features": []})
                return

            # Query column names in the boundary table
            col_info = con.execute("PRAGMA table_info('boundary')").fetchall()
            columns = [col[1] for col in col_info]
            
            select_cols = []
            if "name" in columns:
                select_cols.append("name")
            else:
                select_cols.append("'' AS name")

            if "osm_id" in columns:
                select_cols.append("osm_id")
            else:
                select_cols.append("0 AS osm_id")

            # Determine boundary geom column name
            geom_col = "geom" if "geom" in columns else ("geometry" if "geometry" in columns else None)
            if not geom_col:
                self.send_json({"type": "FeatureCollection", "features": []})
                return

            select_cols.append(f"ST_AsGeoJSON({geom_col}) AS geojson")

            query = f"SELECT {', '.join(select_cols)} FROM boundary"
            rows = con.execute(query).fetchall()
            
            features = []
            for name, osm_id, geojson_str in rows:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geojson_str),
                    "properties": {
                        "name": name,
                        "osm_id": osm_id
                    }
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features
            }
            self.send_json(geojson)
        except Exception as e:
            log.error(f"Error querying boundary from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def serve_runs(self, db_name):
        """Connect to selected DuckDB and return list of runs."""
        if not db_name:
            self.send_error(400, "Missing 'db' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        log.info(f"Querying runs from database: {db_name}")
        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            
            # Check which tables exist in main schema
            existing_tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
            
            if "runs" not in existing_tables:
                self.send_json([])
                return
                
            query = """
                SELECT run_id, source, fetched_at 
                FROM main.runs 
                ORDER BY fetched_at DESC
            """
            rows = con.execute(query).fetchall()
            
            runs = []
            for run_id, source, fetched_at in rows:
                runs.append({
                    "run_id": run_id,
                    "source": source,
                    "fetched_at": fetched_at.isoformat() if hasattr(fetched_at, 'isoformat') else str(fetched_at)
                })
            
            self.send_json(runs)
        except Exception as e:
            log.error(f"Error querying runs from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def init_db_tables(self, con):
        """Ensure all required application tables exist in the database."""
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS main.saved_selections_meta (
                    selection_id VARCHAR PRIMARY KEY,
                    description VARCHAR,
                    saved_at TIMESTAMP
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS main.saved_selections (
                    selection_id VARCHAR,
                    edge_id INTEGER,
                    PRIMARY KEY (selection_id, edge_id)
                )
            """)
        except Exception as e:
            log.error(f"Failed to initialize database tables: {e}")

    def serve_selections(self, db_name):
        """List all saved selections and their metadata from DuckDB."""
        if not db_name:
            self.send_error(400, "Missing 'db' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            
            # Check which tables exist in main schema dynamically
            existing_tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
            
            # If tables don't exist yet, there are zero selections. Return gracefully!
            if "saved_selections_meta" not in existing_tables:
                self.send_json([])
                return
            
            query = """
                SELECT 
                    m.selection_id, 
                    m.description, 
                    m.saved_at, 
                    count(s.edge_id) as edge_count
                FROM main.saved_selections_meta m
                LEFT JOIN main.saved_selections s ON m.selection_id = s.selection_id
                GROUP BY m.selection_id, m.description, m.saved_at
                ORDER BY m.saved_at DESC
            """
            rows = con.execute(query).fetchall()
            
            selections = []
            for sel_id, desc, saved_at, count in rows:
                selections.append({
                    "selection_id": sel_id,
                    "description": desc or "",
                    "saved_at": saved_at.isoformat() if hasattr(saved_at, "isoformat") else str(saved_at),
                    "edge_count": count
                })
            
            self.send_json(selections)
        except Exception as e:
            log.error(f"Error serving selections from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def serve_selection_edges(self, db_name, selection_id):
        """Fetch all roads in a saved selection and return them as a GeoJSON FeatureCollection."""
        if not db_name or not selection_id:
            self.send_error(400, "Missing 'db' or 'selection_id' query parameter")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        con = None
        try:
            con = duckdb.connect(str(db_path), read_only=True)
            con.execute("LOAD spatial")
            
            # Check which tables exist in main schema dynamically
            existing_tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()]
            
            if "saved_selections" not in existing_tables:
                self.send_json({"type": "FeatureCollection", "features": []})
                return
            
            query = """
                SELECT 
                    e.edge_id, 
                    coalesce(e.name, 'Unnamed Road') AS name, 
                    e.highway, 
                    e.length_m,
                    ST_AsGeoJSON(e.geometry) AS geojson
                FROM main.saved_selections s
                JOIN driving.edges e ON s.edge_id = e.edge_id
                WHERE s.selection_id = ?
            """
            rows = con.execute(query, [selection_id]).fetchall()
            
            features = []
            for edge_id, name, highway, length_m, geojson_str in rows:
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(geojson_str),
                    "properties": {
                        "edge_id": edge_id,
                        "name": name,
                        "highway": highway,
                        "length_m": round(length_m, 1)
                    }
                })

            geojson = {
                "type": "FeatureCollection",
                "features": features
            }
            self.send_json(geojson)
        except Exception as e:
            log.error(f"Error serving selection edges from {db_name}: {e}")
            self.send_error(500, f"Database query failed: {e}")
        finally:
            if con:
                con.close()

    def save_selection(self, db_name, data):
        """Save selected edge IDs along with metadata/explanation notes inside DuckDB."""
        selection_id = data.get("selection_id")
        description = data.get("description", "")
        edge_ids = data.get("edge_ids", [])

        if not selection_id:
            self.send_error(400, "Missing 'selection_id'")
            return
        if not edge_ids:
            self.send_error(400, "Missing 'edge_ids' array")
            return

        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        con = None
        try:
            con = duckdb.connect(str(db_path))
            self.init_db_tables(con)
            
            # Start transaction
            con.execute("BEGIN TRANSACTION")
            
            # Clear old records under this name to support overwrites seamlessly
            con.execute("DELETE FROM main.saved_selections_meta WHERE selection_id = ?", [selection_id])
            con.execute("DELETE FROM main.saved_selections WHERE selection_id = ?", [selection_id])
            
            # Insert meta note and timestamp
            con.execute(
                "INSERT INTO main.saved_selections_meta VALUES (?, ?, current_timestamp)", 
                [selection_id, description]
            )
            
            # Insert road segments
            for edge_id in edge_ids:
                con.execute(
                    "INSERT INTO main.saved_selections VALUES (?, ?)",
                    [selection_id, int(edge_id)]
                )
            
            con.execute("COMMIT")
            
            self.send_json({"status": "success", "selection_id": selection_id})
        except Exception as e:
            log.error(f"Error saving selection in {db_name}: {e}")
            if con:
                try:
                    con.execute("ROLLBACK")
                except:
                    pass
            self.send_error(500, f"Failed to save selection: {e}")
        finally:
            if con:
                con.close()

    def delete_selection(self, db_name, selection_id):
        """Remove a saved selection and its metadata from DuckDB."""
        db_path = DB_DIR / db_name
        if not db_path.exists():
            self.send_error(404, f"Database {db_name} not found")
            return

        con = None
        try:
            con = duckdb.connect(str(db_path))
            self.init_db_tables(con)
            
            con.execute("BEGIN TRANSACTION")
            con.execute("DELETE FROM main.saved_selections_meta WHERE selection_id = ?", [selection_id])
            con.execute("DELETE FROM main.saved_selections WHERE selection_id = ?", [selection_id])
            con.execute("COMMIT")
            
            self.send_json({"status": "deleted", "selection_id": selection_id})
        except Exception as e:
            log.error(f"Error deleting selection from {db_name}: {e}")
            if con:
                try:
                    con.execute("ROLLBACK")
                except:
                    pass
            self.send_error(500, f"Failed to delete selection: {e}")
        finally:
            if con:
                con.close()

    def send_json(self, data):
        """Send python dictionary as JSON HTTP response."""
        try:
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log.error(f"Failed to serialize/send JSON response: {e}")
            self.send_error(500, f"Internal JSON serialization error: {e}")


def main():
    port = 8080
    server_address = ("", port)
    
    log.info(f"{'='*60}")
    log.info("  OSM Traffic Sensor Selector Dashboard")
    log.info(f"{'='*60}")
    log.info(f"Workspace Directory : {BASE_DIR}")
    log.info(f"Database Directory  : {DB_DIR}")
    
    # Try starting the server
    try:
        httpd = http.server.HTTPServer(server_address, SensorSelectorHandler)
        url = f"http://localhost:{port}/"
        log.info(f"Dashboard server successfully running at: {url}")
        
        # Open in web browser
        log.info("Opening dashboard in your default web browser...")
        webbrowser.open(url)
        
        log.info("Server is active. Press Ctrl+C to terminate.")
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("\nShutting down dashboard server...")
    except Exception as e:
        log.error(f"Failed to start dashboard server: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
