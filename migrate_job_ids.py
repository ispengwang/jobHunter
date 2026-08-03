#!/usr/bin/env python3
"""Migrate JobHunter's prior job IDs to canonical cross-source IDs.

The command is intentionally one-shot and conservative:

    venv/bin/python migrate_job_ids.py --dry-run
    venv/bin/python migrate_job_ids.py

An actual migration refuses to write when it cannot resolve every event or
attempt ID, and backs up the complete ``data/`` directory before replacing
any CSV.  The dry-run is read-only and reports the exact row/count changes.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
from typing import Iterable

from schema import canonical_job_id, legacy_job_id


STATUS_PRIORITY = {
    "review": 0,
    "ready_to_apply": 1,
    "needs_user": 2,
    "blocked": 2,
    "unavailable": 2,
    "applying": 2,
    "interview": 3,
    "follow_up": 3,
    "rejected": 4,
    "withdrawn": 4,
    "skipped": 5,
    "submitted": 6,
}
CSV_NAMES = (
    "application-dashboard.csv",
    "application-events.csv",
    "application-attempts.csv",
)


@dataclass
class CsvTable:
    path: Path
    fieldnames: list[str]
    rows: list[dict[str, str]]


@dataclass
class MigrationPlan:
    dashboard: CsvTable
    events: CsvTable
    attempts: CsvTable
    dashboard_rows: list[dict[str, str]]
    event_rows: list[dict[str, str]]
    attempt_rows: list[dict[str, str]]
    old_to_new: dict[str, str]
    remapped: dict[str, int]
    merged_dashboard_rows: int
    unresolved: dict[str, list[str]]
    mapping_conflicts: list[str]


def _read_table(path: Path) -> CsvTable:
    if not path.exists():
        return CsvTable(path, [], [])
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return CsvTable(path, list(reader.fieldnames or []), list(reader))


def _status_rank(row: dict[str, str]) -> int:
    return STATUS_PRIORITY.get(row.get("status", ""), -1)


def _timestamp(row: dict[str, str]) -> str:
    return row.get("last_updated_at") or row.get("updated_at") or row.get("timestamp") or row.get("discovered_at") or ""


def _merge_dashboard_group(new_id: str, group: list[dict[str, str]]) -> dict[str, str]:
    """Keep the highest-priority status row and fill its blank fields safely."""
    winner = max(group, key=lambda row: (_status_rank(row), _timestamp(row)))
    merged = dict(winner)
    for row in group:
        for field, value in row.items():
            if not merged.get(field) and value:
                merged[field] = value

    # Preserve every source link while retaining the selected row's primary URL.
    urls: set[str] = set()
    for row in group:
        if row.get("url"):
            urls.add(row["url"])
        urls.update(url for url in (row.get("duplicate_urls") or "").split("; ") if url)
    primary_url = merged.get("url", "")
    merged["duplicate_urls"] = "; ".join(sorted(url for url in urls if url and url != primary_url))
    merged["job_id"] = new_id
    return merged


def _add_mapping(mapping: dict[str, str], old_id: str, new_id: str, conflicts: list[str]) -> None:
    if not old_id:
        return
    previous = mapping.get(old_id)
    if previous and previous != new_id:
        conflicts.append(old_id)
        return
    mapping[old_id] = new_id


def _dashboard_transform(table: CsvTable) -> tuple[list[dict[str, str]], dict[str, str], int, list[str]]:
    mapping: dict[str, str] = {}
    conflicts: list[str] = []
    groups: dict[str, list[dict[str, str]]] = {}
    for original in table.rows:
        row = dict(original)
        new_id = canonical_job_id(row.get("company"), row.get("title"))
        old_id = row.get("job_id", "")
        _add_mapping(mapping, old_id, new_id, conflicts)
        # Also map the old derived value in case a hand-edited row has a stale
        # job_id column but still contains the original identity fields.
        _add_mapping(
            mapping,
            legacy_job_id(row.get("company"), row.get("title"), row.get("url")),
            new_id,
            conflicts,
        )
        row["job_id"] = new_id
        groups.setdefault(new_id, []).append(row)

    transformed = [
        _merge_dashboard_group(new_id, group)
        for new_id, group in groups.items()
    ]
    return transformed, mapping, len(table.rows) - len(transformed), conflicts


def _rewrite_job_ids(
    table: CsvTable,
    mapping: dict[str, str],
) -> tuple[list[dict[str, str]], int, list[str]]:
    transformed: list[dict[str, str]] = []
    changed = 0
    unresolved: list[str] = []
    for original in table.rows:
        row = dict(original)
        old_id = row.get("job_id", "")
        new_id = mapping.get(old_id)
        if new_id is None:
            unresolved.append(old_id)
        elif new_id != old_id:
            row["job_id"] = new_id
            changed += 1
        transformed.append(row)
    return transformed, changed, unresolved


def build_plan(root: Path) -> MigrationPlan:
    data_dir = root / "data"
    dashboard = _read_table(data_dir / CSV_NAMES[0])
    events = _read_table(data_dir / CSV_NAMES[1])
    attempts = _read_table(data_dir / CSV_NAMES[2])
    dashboard_rows, mapping, merged, conflicts = _dashboard_transform(dashboard)
    event_rows, event_changed, event_unresolved = _rewrite_job_ids(events, mapping)
    attempt_rows, attempt_changed, attempt_unresolved = _rewrite_job_ids(attempts, mapping)
    dashboard_changed = sum(
        1 for old, new in zip(dashboard.rows, dashboard_rows)
        if old.get("job_id") != new.get("job_id")
    )
    return MigrationPlan(
        dashboard=dashboard,
        events=events,
        attempts=attempts,
        dashboard_rows=dashboard_rows,
        event_rows=event_rows,
        attempt_rows=attempt_rows,
        old_to_new=mapping,
        remapped={
            "dashboard": dashboard_changed,
            "events": event_changed,
            "attempts": attempt_changed,
        },
        merged_dashboard_rows=merged,
        unresolved={"events": event_unresolved, "attempts": attempt_unresolved},
        mapping_conflicts=conflicts,
    )


def print_plan(plan: MigrationPlan) -> None:
    print("Job ID migration dry-run / plan")
    print(
        f"Dashboard: {len(plan.dashboard.rows)} rows -> {len(plan.dashboard_rows)} "
        f"rows ({plan.merged_dashboard_rows} merge(s), {plan.remapped['dashboard']} ID change(s))"
    )
    print(f"Events: {len(plan.events.rows)} rows ({plan.remapped['events']} ID change(s))")
    print(f"Attempts: {len(plan.attempts.rows)} rows ({plan.remapped['attempts']} ID change(s))")
    print(f"Canonical mappings: {len(plan.old_to_new)}")
    unresolved = sum(len(values) for values in plan.unresolved.values())
    print(f"Unresolved references: {unresolved}")
    print(f"Mapping conflicts: {len(plan.mapping_conflicts)}")


def _write_table(table: CsvTable, rows: Iterable[dict[str, str]]) -> None:
    if not table.fieldnames:
        return
    temporary = table.path.with_name(f".{table.path.name}.migration.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=table.fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(table.path)


def _backup_data(root: Path, requested: Path | None = None) -> Path:
    data_dir = root / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"找不到数据目录: {data_dir}")
    backup = requested or root / f"data-backup-{datetime.now().date().isoformat()}-jobid-v3"
    if backup.exists():
        raise FileExistsError(f"备份目录已存在，为避免覆盖请指定 --backup-dir: {backup}")
    shutil.copytree(data_dir, backup)
    return backup


def migrate(root: Path, backup_dir: Path | None = None) -> Path:
    plan = build_plan(root)
    print_plan(plan)
    unresolved = [item for values in plan.unresolved.values() for item in values if item]
    if unresolved:
        raise ValueError("存在无法映射的事件或 attempt job_id，拒绝写入以保护历史")
    if plan.mapping_conflicts:
        raise ValueError("同一旧 job_id 映射到多个新 ID，拒绝写入以保护历史")
    backup = _backup_data(root, backup_dir)
    _write_table(plan.dashboard, plan.dashboard_rows)
    _write_table(plan.events, plan.event_rows)
    _write_table(plan.attempts, plan.attempt_rows)
    print(f"Migration complete. Backup: {backup}")
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不修改 data/")
    parser.add_argument("--backup-dir", type=Path, help="实际迁移时使用的备份目录")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.dry_run:
        print_plan(build_plan(root))
        return 0
    migrate(root, args.backup_dir.resolve() if args.backup_dir else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
