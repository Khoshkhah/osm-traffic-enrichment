"""
Shared utilities for pipeline_network.py and pipeline_traffic.py.

Provides: logging setup, StepTimer, write_summary, config loading,
          and the Geofabrik country-code → URL lookup table.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

# ── Logging ───────────────────────────────────────────────────────────────────

_step_records: list[tuple[str, str, float]] = []


def setup_logging(name: str, logs_dir: Path, logger_name: str = "pipeline") -> Path:
    """
    Write INFO to console and DEBUG to a timestamped log file.
    Returns the log file path.
    """
    global log
    log = logging.getLogger(logger_name)
    logs_dir.mkdir(exist_ok=True)
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"pipeline_{name}_{ts}.log"

    fmt_file    = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                                    datefmt="%Y-%m-%d %H:%M:%S")
    fmt_console = logging.Formatter("%(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt_console)

    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    log.addHandler(ch)

    log.info(f"Log file: {log_path}")
    return log_path


class StepTimer:
    """Context manager: logs step header/footer and records timing."""
    def __init__(self, label: str):
        self.label = label
        self.t0    = None

    def __enter__(self):
        self.t0 = time.time()
        log.info(f"\n{'─'*60}")
        log.info(f"  {self.label}")
        log.info(f"{'─'*60}")
        log.debug(f"Step started at {datetime.now(timezone.utc).isoformat()}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.time() - self.t0
        if exc_type:
            _step_records.append((self.label, "FAILED", elapsed))
            log.error(f"  FAILED after {elapsed:.1f}s — {exc_val}")
            log.debug(traceback.format_exc())
        else:
            _step_records.append((self.label, "done", elapsed))
            log.info(f"  ✓ Done in {elapsed:.1f}s")
        return False


def write_summary(name: str, total_s: float, log_path: Path) -> None:
    lines = [
        "",
        "=" * 62,
        f"  PIPELINE SUMMARY — {name}",
        "=" * 62,
        f"  {'Step':<36} {'Status':<10} {'Time':>6}",
        "  " + "─" * 58,
    ]
    for label, status, elapsed in _step_records:
        short = label.split("] ", 1)[-1] if "] " in label else label
        lines.append(f"  {short:<36} {status:<10} {elapsed:>5.1f}s")
    lines += [
        "  " + "─" * 58,
        f"  {'TOTAL':<36} {'':10} {total_s:>5.1f}s",
        "=" * 62,
        f"  Log file: {log_path}",
        "=" * 62,
    ]
    log.info("\n".join(lines))


def reset_step_records() -> None:
    """Clear the step record list (call at the start of each pipeline run)."""
    _step_records.clear()


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(path: str | None) -> dict:
    """Load a pipeline config YAML. Returns {} if path is None."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        sys.exit(f"ERROR: config file not found: {p}")
    with open(p) as f:
        return yaml.safe_load(f) or {}


# ── Geofabrik country code → URL ──────────────────────────────────────────────

GEOFABRIK: dict[str, str] = {
    # Europe
    "al": "europe/albania",           "at": "europe/austria",
    "by": "europe/belarus",           "be": "europe/belgium",
    "ba": "europe/bosnia-herzegovina","bg": "europe/bulgaria",
    "hr": "europe/croatia",           "cz": "europe/czech-republic",
    "dk": "europe/denmark",           "ee": "europe/estonia",
    "fi": "europe/finland",           "fr": "europe/france",
    "de": "europe/germany",           "gr": "europe/greece",
    "hu": "europe/hungary",           "is": "europe/iceland",
    "ie": "europe/ireland",           "it": "europe/italy",
    "lv": "europe/latvia",            "lt": "europe/lithuania",
    "lu": "europe/luxembourg",        "md": "europe/moldova",
    "me": "europe/montenegro",        "nl": "europe/netherlands",
    "mk": "europe/north-macedonia",   "no": "europe/norway",
    "pl": "europe/poland",            "pt": "europe/portugal",
    "ro": "europe/romania",           "rs": "europe/serbia",
    "sk": "europe/slovakia",          "si": "europe/slovenia",
    "es": "europe/spain",             "se": "europe/sweden",
    "ch": "europe/switzerland",       "tr": "europe/turkey",
    "ua": "europe/ukraine",           "gb": "europe/great-britain",
    "ru": "europe/russia",
    # North America
    "ca": "north-america/canada",     "mx": "north-america/mexico",
    "us": "north-america/us",
    # South America
    "ar": "south-america/argentina",  "br": "south-america/brazil",
    "cl": "south-america/chile",      "co": "south-america/colombia",
    "pe": "south-america/peru",
    # Asia
    "cn": "asia/china",               "in": "asia/india",
    "id": "asia/indonesia",           "ir": "asia/iran",
    "il": "asia/israel",              "jp": "asia/japan",
    "kz": "asia/kazakhstan",          "my": "asia/malaysia",
    "np": "asia/nepal",               "pk": "asia/pakistan",
    "ph": "asia/philippines",         "sa": "asia/saudi-arabia",
    "kr": "asia/south-korea",         "lk": "asia/sri-lanka",
    "tw": "asia/taiwan",              "th": "asia/thailand",
    "ae": "asia/united-arab-emirates","vn": "asia/vietnam",
    # Africa
    "eg": "africa/egypt",             "et": "africa/ethiopia",
    "gh": "africa/ghana",             "ke": "africa/kenya",
    "ma": "africa/morocco",           "ng": "africa/nigeria",
    "za": "africa/south-africa",      "tz": "africa/tanzania",
    "ug": "africa/uganda",
    # Oceania
    "au": "australia-oceania/australia",
    "nz": "australia-oceania/new-zealand",
}


def country_url_from_code(country_code: str) -> str | None:
    path = GEOFABRIK.get(country_code.lower())
    if not path:
        return None
    return f"https://download.geofabrik.de/{path}-latest.osm.pbf"
