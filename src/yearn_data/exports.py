"""CSV exports for analysis outputs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .storage import from_json


def latest_run_id(conn, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM analysis_runs WHERE name=? AND status='complete' ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        raise ValueError(f"no completed analysis run for {name}")
    return int(row["id"])


def export_analysis(conn, name: str, out_dir: str | Path = "exports", run_id: int | None = None) -> list[Path]:
    rid = latest_run_id(conn, name) if run_id is None else run_id
    run = conn.execute(
        "SELECT * FROM analysis_runs WHERE id=? AND name=? AND status='complete'",
        (rid, name),
    ).fetchone()
    if run is None:
        raise ValueError(f"analysis run {rid} is not a completed {name} run")
    context = {key: run[key] for key in ("id", "name", "started_at", "completed_at", "status")}
    context["params"] = from_json(run["params_json"])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    output_names = [
        row["name"]
        for row in conn.execute(
            "SELECT DISTINCT name FROM analysis_outputs WHERE run_id=? ORDER BY name",
            (rid,),
        )
    ]
    for output_name in output_names:
        first = conn.execute(
            """
            SELECT rowid, row_json
            FROM analysis_outputs
            WHERE run_id=? AND name=?
            ORDER BY rowid
            LIMIT 1
            """,
            (rid, output_name),
        ).fetchone()
        if not first:
            continue
        first_item = from_json(first["row_json"])
        path = out / f"{output_name}.csv"
        columns: list[str] = list(first_item)
        with path.open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(first_item)
            rows = conn.execute(
                """
                SELECT row_json
                FROM analysis_outputs
                WHERE run_id=? AND name=? AND rowid > ?
                ORDER BY rowid
                """,
                (rid, output_name, int(first["rowid"])),
            )
            for row in rows:
                writer.writerow(from_json(row["row_json"]))
        written.append(path)
    context["files"] = [path.name for path in written]
    context_path = out / "context.json"
    context_path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n")
    written.append(context_path)
    return written
