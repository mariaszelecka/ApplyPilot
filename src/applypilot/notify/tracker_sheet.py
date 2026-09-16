"""Appends a row to your own manual job-search tracker spreadsheet after
ApplyPilot submits a real application.

Optional. Set APPLYPILOT_TRACKER_XLSX to the full path of an .xlsx file to
enable it; if unset, this module does nothing. The workbook is expected to
have one tab per month (named by month, full or 3-letter) with columns
A=Date B=Firm C=Role D=Link E=Location F=source-tag.

Best-effort and must never break the apply pipeline: any failure here is
logged and swallowed, never raised, and the sheet is written defensively --
see _is_file_locked below for why.

RETRY QUEUE: a file open in Excel at the moment of a successful application
is a normal, frequent situation (confirmed happening in practice -- an
application succeeded while the sheet was open and the row was silently
skipped with only a log line to show for it). Rather than lose that row
permanently, it's queued to a small local JSON file and retried on every
future call to log_application() -- so the next application (or a periodic
flush_pending()) catches up on it once the sheet is closed again.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def _tracker_path() -> Path | None:
    raw = os.environ.get("APPLYPILOT_TRACKER_XLSX", "").strip()
    return Path(raw) if raw else None


def _queue_path() -> Path:
    from applypilot.config import APP_DIR
    return APP_DIR / "tracker_pending.json"


# Matched case-insensitively against a target month's full name or 3-letter
# abbreviation -- never auto-created, since guessing at a new month's
# formatting/layout risks doing it wrong. See _write_row().
_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November",
    12: "December",
}

_SOURCE_TAG = "Applypilot"


def _is_file_locked(path: Path) -> bool:
    """True if Excel (or anything else) currently has the file open.

    Office apps drop a hidden `~$<name>` lock file next to a workbook while
    it's open. Writing to the real file at the same time is a real risk: the
    write can be rejected outright, or silently discarded the next time Excel
    saves, overwriting the disk copy with whatever is still in Excel's memory
    -- the added row would just vanish with no error. A file left open in
    Excel is a normal scenario this has to handle, not an edge case.
    """
    lock_path = path.with_name(f"~${path.name}")
    return lock_path.exists()


def _sheet_for_month(sheetnames: list[str], dt: datetime) -> str | None:
    full = _MONTH_NAMES[dt.month].lower()
    abbr = full[:3]
    for name in sheetnames:
        low = name.strip().lower()
        if low == full or low == abbr:
            return name
    return None


def _next_row(ws, cols: str = "ABCDEF") -> int:
    """First row (after the header) with every one of `cols` empty."""
    row = 2
    while any(ws[f"{c}{row}"].value not in (None, "") for c in cols):
        row += 1
    return row


def _load_queue() -> list[str]:
    path = _queue_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_queue(urls: list[str]) -> None:
    try:
        _queue_path().write_text(json.dumps(urls, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("tracker_sheet: could not persist pending queue: %s", e)


def _write_row(tracker_path: Path, url: str) -> bool:
    """Attempt to write one row. Returns True on success, False on any
    skip/failure (locked file, missing tab, missing DB row, etc.)."""
    from applypilot.database import get_connection
    conn = get_connection()
    row = conn.execute(
        "SELECT title, company, location, url, application_url "
        "FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    if not row:
        log.warning("tracker_sheet: no DB row for %s -- dropping from queue.", url)
        return True  # nothing to retry -- treat as "handled" so it's not requeued forever
    job = dict(row)

    if not tracker_path.exists():
        log.warning("tracker_sheet: %s not found -- skipping.", tracker_path)
        return False

    if _is_file_locked(tracker_path):
        return False

    import openpyxl
    now = datetime.now(timezone.utc)
    wb = openpyxl.load_workbook(tracker_path)
    sheet_name = _sheet_for_month(wb.sheetnames, now)
    if not sheet_name:
        log.warning(
            "tracker_sheet: no tab found for %s among %s -- skipping "
            "rather than guessing a new tab's layout.",
            _MONTH_NAMES[now.month], wb.sheetnames,
        )
        return True  # not a locked-file case -- retrying won't help, don't requeue

    ws = wb[sheet_name]
    r = _next_row(ws)
    link = job.get("application_url") or job.get("url")
    title = (job.get("title") or "").strip()

    ws[f"A{r}"] = now.strftime("%d.%m.%Y")
    ws[f"B{r}"] = job.get("company") or ""
    title_cell = ws[f"C{r}"]
    title_cell.value = title
    if link:
        title_cell.hyperlink = link
    ws[f"D{r}"] = link or ""
    if job.get("location"):
        ws[f"E{r}"] = job["location"]
    tag_cell = ws[f"F{r}"]
    tag_cell.value = _SOURCE_TAG
    tag_cell.font = tag_cell.font.copy(bold=True)

    wb.save(tracker_path)
    log.info("tracker_sheet: logged '%s' @ '%s' to %s (row %d)",
             title, job.get("company"), sheet_name, r)
    return True


def flush_pending() -> int:
    """Retry every queued row (from past locked-file skips). Call this
    opportunistically -- at the start of a daily/poll run is a good spot --
    so a row queued while the sheet was open gets written once it's closed,
    even on a day with no new application to trigger it.

    Returns:
        Number of rows successfully written.
    """
    tracker_path = _tracker_path()
    queue = _load_queue()
    if tracker_path is None or not queue:
        return 0

    written = 0
    still_pending: list[str] = []
    for url in queue:
        try:
            ok = _write_row(tracker_path, url)
        except Exception as e:
            log.warning("tracker_sheet: retry failed for %s: %s", url, e)
            ok = False
        if ok:
            written += 1
        else:
            still_pending.append(url)

    _save_queue(still_pending)
    if written:
        log.info("tracker_sheet: flushed %d pending row(s), %d still waiting.",
                 written, len(still_pending))
    return written


def log_application(url: str) -> bool:
    """Append one row for a job that was just successfully submitted.

    Also opportunistically flushes any previously-queued rows first, so a
    later successful application helps clear a backlog from an earlier one
    that hit a locked file.

    Args:
        url: The job's canonical URL (jobs.url), used to look up its details.

    Returns:
        True if this job's row was written (now or already queued -- either
        way it will land eventually). False only if the feature isn't
        configured or the DB has no record of this URL.
    """
    tracker_path = _tracker_path()
    if tracker_path is None:
        return False  # feature not configured -- silent no-op

    try:
        flush_pending()

        ok = _write_row(tracker_path, url)
        if ok:
            return True

        # Locked (or some other transient issue) -- queue it rather than lose it.
        queue = _load_queue()
        if url not in queue:
            queue.append(url)
            _save_queue(queue)
            log.warning(
                "tracker_sheet: %s is open (or otherwise unwritable) -- queued "
                "for %s, will retry on the next application or flush.",
                tracker_path.name, url,
            )
        return True

    except Exception as e:
        # Never let a spreadsheet problem take down a real apply run.
        log.warning("tracker_sheet: failed to log %s: %s", url, e)
        return False
