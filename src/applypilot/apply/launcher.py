"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, prompt as prompt_mod
from applypilot.apply.chrome import (
    find_free_cdp_port,
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()


_CLAUDE_EXE: str | None = None


def _resolve_claude_exe() -> str:
    """Resolve the 'claude' CLI to its full executable path.

    On Windows, npm installs it as a claude.cmd shim. subprocess.Popen(["claude", ...])
    with shell=False goes straight to CreateProcess, which -- unlike a real shell --
    does not try PATHEXT extensions, so the bare name fails with WinError 2 even
    though `claude` resolves fine interactively. shutil.which() does the same PATHEXT
    search a shell would and returns the concrete path, which CreateProcess can launch
    directly without needing shell=True (which would complicate the stdin/stdout piping
    this module relies on for streaming JSON).
    """
    global _CLAUDE_EXE
    if _CLAUDE_EXE is None:
        resolved = shutil.which("claude")
        if not resolved:
            raise RuntimeError(
                "Could not find 'claude' on PATH. Ensure the Claude Code CLI is installed "
                "and available (e.g. `npm install -g @anthropic-ai/claude-code`)."
            )
        _CLAUDE_EXE = resolved
    return _CLAUDE_EXE

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

# Pinned, not "@latest". With @latest, npx re-resolves the package against the
# npm registry on every single apply attempt, so a momentary registry or network
# blip means the MCP server never starts -- and the agent then runs with NO
# browser tools at all, which looks like a mysterious application failure rather
# than a network error. Seen in practice: Chrome launches, the CV is tailored,
# and the agent aborts because browser_navigate doesn't exist. A pinned
# version resolves from the local npx cache instead.
_PLAYWRIGHT_MCP_VERSION = "0.0.80"


def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    f"@playwright/mcp@{_PLAYWRIGHT_MCP_VERSION}",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, dry_run: bool = False) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        dry_run: If True, pull from the review queue (apply_status IS NULL
            or 'failed' -- same pool as before). If False, this is a REAL,
            submitting run: only jobs a human has explicitly approved
            (apply_status='approved', set via `applypilot apply --approve`)
            are eligible. This is the actual enforcement of "never apply
            without approval" -- a live run cannot reach a job that hasn't
            been through a --dry-run review pass and been approved.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        if target_url:
            status_clause = "" if dry_run else "AND apply_status = 'approved'"
            # Exact match first. Aggregator sites (Indeed, etc.) share one
            # base path across every posting and only differ by a query
            # param (e.g. ?jk=...), so a same-site LIKE fallback on the
            # query-stripped base path can match a *different* approved job
            # -- only fall back to that loose match if nothing exact is found.
            row = conn.execute(f"""
                SELECT url, title, company, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE (url = ? OR application_url = ?)
                  AND tailored_resume_path IS NOT NULL
                  AND apply_status != 'in_progress'
                  {status_clause}
                LIMIT 1
            """, (target_url, target_url)).fetchone()
            if row is None:
                like = f"%{target_url.split('?')[0].rstrip('/')}%"
                row = conn.execute(f"""
                    SELECT url, title, company, site, application_url, tailored_resume_path,
                           fit_score, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE (application_url LIKE ? OR url LIKE ?)
                      AND tailored_resume_path IS NOT NULL
                      AND apply_status != 'in_progress'
                      {status_clause}
                    LIMIT 1
                """, (like, like)).fetchone()
        else:
            blocked_sites, blocked_patterns = _load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join(f"AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            # dry_run=True: the review queue -- unreviewed/previously-failed
            # jobs, PLUS 'requested' ones (a human replied "yes" to that job
            # number in the digest email -- see notify/digest.py's
            # check_email_approvals). 'requested' jobs are prioritized first
            # in ORDER BY so replying to the digest actually gets a job
            # reviewed on the very next run, not whenever its raw fit_score
            # would otherwise put it in the queue. dry_run=False: ONLY jobs
            # a human has approved -- see the docstring above.
            if dry_run:
                status_clause = "(apply_status IS NULL OR apply_status = 'failed' OR apply_status = 'requested')"
                order_clause = "(apply_status = 'requested') DESC, fit_score DESC, url"
            else:
                # A live run also re-picks up a job that already went through
                # approval and then failed -- e.g. an MCP-startup timing race,
                # not something that needs a human. This does NOT reopen jobs
                # that need a human: mark_result() jumps apply_attempts straight
                # to 99 for a PERMANENT_FAILURES reason (login wall, CAPTCHA,
                # expired, ...), so the apply_attempts < max_apply_attempts
                # clause below already excludes those -- only a job that failed
                # for a transient reason, within its attempt budget, re-qualifies.
                status_clause = "(apply_status = 'approved' OR apply_status = 'failed')"
                order_clause = "fit_score DESC, url"
            row = conn.execute(f"""
                SELECT url, title, company, site, application_url, tailored_resume_path,
                       fit_score, location, full_description, cover_letter_path
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND {status_clause}
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  {site_clause}
                  {url_clauses}
                ORDER BY {order_clause}
                LIMIT 1
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

        if not row:
            conn.rollback()
            return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        apply_url = row["application_url"] or row["url"]
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, row["url"]))
        conn.commit()

        return dict(row)
    except Exception:
        conn.rollback()
        raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None, review_notes: str | None = None) -> None:
    """Update a job's apply status in the database.

    status='review_ready' is a DRY RUN outcome -- it must never set
    applied_at or apply_status='applied' (that would falsely mark a job as
    submitted when nothing was, and permanently remove it from every future
    queue). It sets apply_status='pending_review' instead, storing the
    agent's filled-form summary in review_notes for a human to read via
    `applypilot apply --list-pending`, and doesn't touch apply_attempts --
    review passes aren't a real attempt.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
        conn.commit()
        # Best-effort: mirror the submission into the user's manual tracker
        # (Job search.xlsx). Never let this affect the apply pipeline itself.
        try:
            from applypilot.notify.tracker_sheet import log_application
            log_application(url)
        except Exception:
            logger.exception("tracker_sheet logging failed for %s", url)
    elif status == "review_ready":
        conn.execute("""
            UPDATE jobs SET apply_status = 'pending_review', apply_error = NULL,
                           agent_id = NULL, apply_duration_ms = ?,
                           apply_task_id = ?, review_notes = ?
            WHERE url = ?
        """, (duration_ms, task_id, review_notes, url))
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (status, error or "unknown", duration_ms, task_id, url))
    conn.commit()


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Review / approval gate (human-in-the-loop before any real submission)
# ---------------------------------------------------------------------------

def list_pending_review() -> list[dict]:
    """Jobs a dry-run pass has filled out and is waiting on a human to
    review, ordered by fit_score. Read review_notes before approving."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, title, company, site, fit_score, review_notes, last_attempted_at
        FROM jobs
        WHERE apply_status = 'pending_review'
        ORDER BY fit_score DESC, last_attempted_at DESC
    """).fetchall()
    if not rows:
        return []
    columns = rows[0].keys()
    return [dict(zip(columns, row)) for row in rows]


def approve_job(url: str) -> bool:
    """Approve one pending_review job for a real (live) submission.

    Only jobs currently in 'pending_review' can be approved -- this can't
    be used to bypass the review step for a job that was never dry-run
    first.

    Returns:
        True if a matching pending_review job was approved, False otherwise.
    """
    conn = get_connection()
    # Exact match first. Aggregator sites (Indeed, etc.) share one base path
    # across every posting and only differ by a query param (e.g. ?jk=...),
    # so a same-site LIKE fallback on the query-stripped base path can match
    # every other pending_review job from that site too -- only fall back to
    # that loose match (and only ever touch one row) if nothing exact is found.
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = 'approved'
        WHERE apply_status = 'pending_review' AND (url = ? OR application_url = ?)
    """, (url, url))
    if cursor.rowcount == 0:
        like = f"%{url.split('?')[0].rstrip('/')}%"
        row = conn.execute("""
            SELECT url FROM jobs
            WHERE apply_status = 'pending_review'
              AND (application_url LIKE ? OR url LIKE ?)
            LIMIT 1
        """, (like, like)).fetchone()
        if row is not None:
            cursor = conn.execute(
                "UPDATE jobs SET apply_status = 'approved' WHERE url = ? AND apply_status = 'pending_review'",
                (row[0],),
            )
    conn.commit()
    return cursor.rowcount > 0


def approve_all_pending() -> int:
    """Approve every job currently in pending_review. Use after reading
    through `applypilot apply --list-pending`, not as a way to skip reading it.

    Returns:
        Number of jobs approved.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = 'approved' WHERE apply_status = 'pending_review'
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Uses dry_run=True's queue matching (any tailored job, not just approved
    ones) -- this never submits anything itself, it just writes a prompt
    file and prints a command for a human to run by hand, so it isn't
    subject to the approved-only restriction that gates real (live) runs.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id, dry_run=True)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
        conn.commit()
        try:
            from applypilot.notify.tracker_sheet import log_application
            log_application(url)
        except Exception:
            logger.exception("tracker_sheet logging failed for %s", url)
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Deliberately excludes 'pending_review', 'approved', and 'requested' --
    those hold real human review state (or a human's explicit go-ahead, via
    CLI or an email reply) and must never be silently wiped by a
    retry-the-failures sweep.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status NOT IN
              ('applied', 'in_progress', 'pending_review', 'approved', 'requested'))
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int, str | None]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms, review_notes). review_notes is
        only non-None when status is 'review_ready' (dry_run outcome) --
        the agent's full response, including its written summary of every
        field/answer it filled, for a human to read before ever approving a
        real submission. Status is one of: 'applied', 'review_ready',
        'expired', 'captcha', 'login_issue', 'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        _resolve_claude_exe(),
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        # Without this, Claude Code also loads whatever OTHER MCP servers are
        # configured at the user/project level (Notion, Calendar, Drive,
        # Composio, ...) alongside the two this worker actually needs. Root
        # cause of a real failure (2026-09-15): a job failed with
        # "no browser tooling" because those unrelated servers loaded and
        # crowded out/masked playwright, not because of the MCP-startup race
        # this prompt already retries for. It's also a scope leak in its own
        # right -- an unattended agent reading untrusted job-posting text has
        # no business being able to reach Notion/Calendar/Drive/Gmail-beyond-
        # what's explicitly allowed. This restricts it to exactly the two
        # servers in _make_mcp_config: playwright and gmail (itself already
        # narrowed by --disallowedTools above).
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            # send_email blocked deliberately: for an agent that reads untrusted
            # job-posting text, the ability to send mail is a data-exfiltration
            # path that outweighs its benefit. The email-only application
            # fallback that used it has been removed from the prompt to match
            # (see prompt.py step 4) -- a genuinely email-only posting now fails
            # cleanly with RESULT:FAILED:email_only_application instead of
            # reaching for a blocked tool.
            "mcp__gmail__send_email,"
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    worker_dir = reset_worker_dir(worker_id)

    company_display = job.get("company") or job.get("site", "")
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=company_display, score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {company_display}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {company_display}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=300)
        returncode = proc.returncode
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000), None

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        if "RESULT:REVIEW_READY" in output:
            add_event(f"[W{worker_id}] REVIEW_READY ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status="review_ready",
                         last_action=f"REVIEW_READY ({elapsed}s)")
            # The agent's full response (including its written summary of
            # every field/answer it filled) is the review artifact -- a
            # human reads this via `applypilot apply --list-pending` before
            # ever approving a real submission.
            return "review_ready", duration_ms, output

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"]:
            if f"RESULT:{result_status}" in output:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms, None

        if "RESULT:FAILED" in output:
            for out_line in output.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms, None
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms, None
            return "failed:unknown", duration_ms, None

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms, None

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms, None
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms, None
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required", "ats_login_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    # Verify the port is actually free. A foreign process already holding it
    # would otherwise hand Playwright the wrong browser instead of failing
    # outright (see chrome.py's BASE_CDP_PORT note).
    port = find_free_cdp_port(BASE_CDP_PORT + worker_id)

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, dry_run=dry_run)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms, review_notes = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            elif result == "review_ready":
                # Dry-run outcome -- NOT applied. Sits in pending_review
                # until a human reads the notes and explicitly approves it
                # (`applypilot apply --approve <url>`) before any real,
                # submitting run can ever pick it up.
                mark_result(job["url"], "review_ready", duration_ms=duration_ms,
                            review_notes=review_notes)
                applied += 1  # counts toward this run's "done" total for the dashboard
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            # A CAPTCHA is the one failure a human can finish in seconds --
            # but only if the browser is still on the filled-in form. Closing
            # it here threw that away: every field was completed, then the
            # window vanished before anyone could tick the box. So on a
            # visible (non-headless) run, leave Chrome open and hand it over.
            hand_over = (
                not headless
                and isinstance(locals().get("result"), str)
                and locals().get("result", "").startswith("captcha")
            )
            if hand_over:
                chrome.keep_browser_open()  # also stops the atexit sweep
                add_event(f"[W{worker_id}] CAPTCHA -- browser left open for you")
                logger.warning(
                    "Form is filled and waiting on the CAPTCHA. The Chrome window has "
                    "been left open -- tick the checkbox, press Submit, then close it."
                )
            elif chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    run_start = datetime.now(timezone.utc).isoformat()

    config.ensure_dirs()
    console = Console()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
        _notify_run_results(run_start, console)


def _notify_run_results(run_start: str, console: Console) -> None:
    """Email a summary of anything from this run that needs human attention:
    CAPTCHA blocks (go solve these yourself), jobs ready for review (from a
    --dry-run pass), real applies, and outright failures. Scoped to this run
    via last_attempted_at, which acquire_job() stamps when a job is claimed.
    Best-effort -- a notification failure should never crash the run that
    already completed."""
    try:
        conn = get_connection()

        def _fetch(status: str, extra_cols: str = "") -> list[dict]:
            rows = conn.execute(f"""
                SELECT url, title, company, site, fit_score, application_url {extra_cols}
                FROM jobs WHERE apply_status = ? AND last_attempted_at >= ?
            """, (status, run_start)).fetchall()
            if not rows:
                return []
            columns = rows[0].keys()
            return [dict(zip(columns, row)) for row in rows]

        captcha_jobs = _fetch("captcha")
        reviewed_jobs = _fetch("pending_review", ", review_notes")
        applied_jobs = _fetch("applied", ", tailored_resume_path, cover_letter_path")
        failed_jobs = _fetch("failed", ", apply_error, tailored_resume_path, cover_letter_path")

        if not (captcha_jobs or reviewed_jobs or applied_jobs or failed_jobs):
            return

        from applypilot.notify.digest import send_apply_run_notification
        result = send_apply_run_notification(captcha_jobs, reviewed_jobs, applied_jobs, failed_jobs)
        if result.get("sent"):
            console.print(
                f"[dim]Notified by email: {len(captcha_jobs)} captcha, "
                f"{len(reviewed_jobs)} for review, {len(applied_jobs)} applied, "
                f"{len(failed_jobs)} failed.[/dim]"
            )
        elif result.get("error"):
            console.print(f"[yellow]Could not send apply notification email: {result['error']}[/yellow]")
    except Exception:
        logger.exception("Failed to send apply run notification email")
