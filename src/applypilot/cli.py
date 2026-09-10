"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def daily(
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    no_email: bool = typer.Option(False, "--no-email", help="Skip sending the digest email."),
    apply_review_limit: int = typer.Option(
        0, "--apply-review-limit",
        help=(
            "Max jobs to auto-fill for a dry-run review each day. Defaults to 0 "
            "(off): the single-gate flow submits what you approved by digest "
            "reply, so a separate no-submit review pass isn't part of it. Set "
            "a number to preview forms without submitting."
        ),
    ),
    no_apply_review: bool = typer.Option(
        False, "--no-apply-review", help="Skip the automatic apply review pass entirely.",
    ),
    live_apply_limit: int = typer.Option(
        10, "--live-apply-limit",
        help="Max jobs to actually submit each day, from what's already approved (0 disables).",
    ),
    no_live_apply: bool = typer.Option(
        False, "--no-live-apply", help="Skip the automatic live-submit stage entirely.",
    ),
) -> None:
    """Run the daily automated pipeline: discover -> enrich -> score, email one
    digest of matches, and -- for whatever you approved by replying to a
    previous digest -- tailor -> cover -> pdf, then submit those applications.

    SINGLE-GATE FLOW: search -> match -> ONE email -> you reply with the
    numbers you want to apply to -> ApplyPilot tailors a CV + cover letter for
    exactly those and submits them on the next run. There is no second
    confirmation email. A job you never named by number is never tailored and
    never submitted.

    The approval itself is still enforced in acquire_job()'s query, not here:
    a live (submitting) run can only ever draw jobs with
    apply_status='approved', which is set only by your own digest reply (see
    notify.digest.check_email_approvals) or an explicit
    `applypilot apply --approve`. Automating this stage therefore doesn't
    weaken the gate -- nothing is submitted the day it's discovered, only
    after you've picked it out of a digest.

    --apply-review-limit (default 0, off) can still run a no-submit dry pass
    that fills forms and screenshots them for inspection; it's not part of
    the single-gate flow.

    Intended for a scheduled task (see run_daily.ps1). Reading your replies
    is best-effort and never blocks the rest of the run.
    """
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.pipeline import run_pipeline

    check_tier(2, "AI scoring/tailoring")

    try:
        from applypilot.notify.digest import check_email_approvals
        approval_result = check_email_approvals()
        if approval_result.get("error"):
            console.print(f"[yellow]Email approval check failed: {approval_result['error']}[/yellow]")
        elif approval_result["requested"] or approval_result["approved"]:
            console.print(
                f"[bold blue]Email approvals found:[/bold blue] "
                f"{len(approval_result['requested'])} approved by digest reply, "
                f"{len(approval_result['approved'])} approved for submission."
            )
    except Exception as e:
        log.exception("Email approval check failed")
        console.print(f"[yellow]Email approval check failed: {e}[/yellow]")

    # Fresh discovery + matching only -- no tailoring yet, since nothing has
    # been approved for today's new matches until the digest goes out and
    # you reply.
    result = run_pipeline(
        stages=["discover", "enrich", "score"],
        min_score=min_score,
        workers=workers,
        validation_mode="normal",
    )

    # Everything you approved (by digest reply just now, or on an earlier run
    # whose tailoring didn't finish) that still has no CV. Read from the DB
    # rather than only check_email_approvals()'s return value, so an approved
    # job is never silently dropped because a previous run died mid-way.
    from applypilot.database import get_connection as _get_conn
    _conn = _get_conn()
    requested_urls = [
        r[0] for r in _conn.execute(
            "SELECT url FROM jobs WHERE apply_status = 'approved' "
            "AND tailored_resume_path IS NULL"
        ).fetchall()
    ]

    if requested_urls:
        console.print(
            f"\n[bold blue]Preparing approved jobs[/bold blue] "
            f"({len(requested_urls)} job(s): tailoring CV + cover letter)"
        )
        try:
            from applypilot.scoring.tailor import run_tailoring
            from applypilot.scoring.cover_letter import run_cover_letters
            from applypilot.scoring.pdf import batch_convert

            run_tailoring(job_urls=requested_urls, validation_mode="normal")
            run_cover_letters(job_urls=requested_urls, validation_mode="normal")
            batch_convert()
        except Exception as e:
            log.exception("Tailoring approved jobs failed")
            console.print(f"[yellow]Tailoring approved jobs failed: {e}[/yellow]")

    # Re-score anything that qualifies for a digest but predates the
    # WHY/MISSING fields. Without this, older jobs scored before those columns
    # existed reach the inbox with prose instead of bullets and "Not assessed"
    # where the gap should be.
    try:
        from applypilot.database import get_connection as _gc
        _c = _gc()
        stale = [
            r[0] for r in _c.execute(
                "SELECT url FROM jobs WHERE fit_score >= ? AND digest_sent_at IS NULL "
                "AND (score_why IS NULL OR score_why = '') "
                "AND discovered_at >= datetime('now', '-4 days') "
                "AND (apply_status IS NULL OR apply_status != 'applied')",
                (min_score,),
            ).fetchall()
        ]
        if stale:
            console.print(
                f"[bold blue]Re-scoring[/bold blue] {len(stale)} job(s) missing the "
                f"why/gap breakdown before the digest..."
            )
            from applypilot.scoring.scorer import rescore_urls
            rescore_urls(stale)
    except Exception as e:
        log.exception("Pre-digest rescore failed")
        console.print(f"[yellow]Pre-digest rescore failed: {e}[/yellow]")

    if no_email:
        console.print(
            "\n[bold yellow]Digest email skipped (--no-email).[/bold yellow] "
            "Nothing was submitted -- ApplyPilot never applies automatically."
        )
    else:
        from applypilot.notify.digest import send_daily_digest
        digest_result = send_daily_digest(min_score=min_score)
        if digest_result.get("error"):
            console.print(f"\n[red]Digest email failed:[/red] {digest_result['error']}")
        elif digest_result["jobs"] == 0:
            console.print("\n[dim]No new job matches to email today.[/dim]")
        else:
            console.print(
                f"\n[bold green]Digest emailed:[/bold green] {digest_result['jobs']} new job match(es). "
                "Nothing tailored or submitted yet -- reply with the numbers you approve."
            )

    # Apply review pass: Tier 3 only (Claude CLI + Chrome), always --dry-run,
    # always headless. Best-effort -- a failure here must never fail the
    # whole daily run, since the digest (the established, reliable part)
    # already succeeded above.
    if no_apply_review or apply_review_limit <= 0:
        console.print("[dim]Apply review pass skipped.[/dim]")
    else:
        from applypilot.config import get_tier
        if get_tier() < 3:
            console.print(
                "[dim]Apply review pass skipped -- Tier 3 (Claude Code CLI + Chrome) not available.[/dim]"
            )
        else:
            try:
                from applypilot.apply.launcher import main as apply_main
                console.print(
                    f"\n[bold blue]Apply review pass[/bold blue] "
                    f"(up to {apply_review_limit} jobs, headless, dry-run -- never submits)"
                )
                apply_main(
                    limit=apply_review_limit,
                    min_score=min_score,
                    headless=True,
                    dry_run=True,
                    workers=1,
                )
            except Exception as e:
                log.exception("Apply review pass failed")
                console.print(f"[yellow]Apply review pass failed (digest already sent): {e}[/yellow]")

    # Live-submit stage: Tier 3 only, headless, dry_run=False -- but this can
    # ONLY ever pick up apply_status='approved' jobs (acquire_job()'s query,
    # not this command, enforces that), so it never submits anything you
    # haven't already reviewed and approved yourself on a prior run.
    # Best-effort, same as the review pass above.
    if no_live_apply or live_apply_limit <= 0:
        console.print("[dim]Live-submit stage skipped.[/dim]")
    else:
        from applypilot.config import get_tier
        if get_tier() < 3:
            console.print(
                "[dim]Live-submit stage skipped -- Tier 3 (Claude Code CLI + Chrome) not available.[/dim]"
            )
        else:
            try:
                from applypilot.apply.launcher import main as apply_main
                console.print(
                    f"\n[bold red]Live-submit stage[/bold red] "
                    f"(up to {live_apply_limit} jobs, headless, approved-only -- submits for real)"
                )
                apply_main(
                    limit=live_apply_limit,
                    min_score=min_score,
                    headless=True,
                    dry_run=False,
                    workers=1,
                )
            except Exception as e:
                log.exception("Live-submit stage failed")
                console.print(f"[yellow]Live-submit stage failed (digest already sent): {e}[/yellow]")

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def poll(
    limit: int = typer.Option(
        5, "--limit", "-l",
        help="Max applications to submit in one poll (bounds spend per tick).",
    ),
    no_apply: bool = typer.Option(
        False, "--no-apply",
        help="Read replies and tailor the approved jobs, but don't submit anything.",
    ),
) -> None:
    """Act on digest replies now, instead of waiting for tomorrow's daily run.

    Designed to run on a short interval (every ~15 min) alongside the once-a-day
    `daily` command. It does NO discovery and NO scoring, so it is free when
    idle: reading the mailbox costs nothing, and money is only spent once you
    have actually approved something.

    WHAT IT APPLIES TO: only jobs you approved yourself, by replying to a digest
    with their numbers. This command adds no job-selection logic of its own --
    it reuses the same apply_status='approved' gate enforced inside
    acquire_job(), so it can never reach a job you didn't name. It just acts on
    your approval in minutes rather than up to a day later.

    Steps: read unread replies -> tailor CV + cover letter for anything newly
    approved that lacks one -> submit up to --limit of them.
    """
    _bootstrap()

    from applypilot.config import APP_DIR, get_tier
    from applypilot.database import get_connection

    # Never run while a daily run is active -- they would contend for the DB and,
    # worse, drive two Chrome/apply stages at once. Own lock, plus a check on
    # daily's. A lock whose PID is gone is stale and gets taken over.
    def _stale(lock_path) -> bool:
        if not lock_path.exists():
            return True
        try:
            pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            return True
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    daily_lock = APP_DIR / "daily.lock"
    poll_lock = APP_DIR / "poll.lock"
    if not _stale(daily_lock):
        console.print("[dim]Daily run in progress -- skipping this poll.[/dim]")
        return
    if not _stale(poll_lock):
        console.print("[dim]Another poll is still running -- skipping.[/dim]")
        return

    poll_lock.write_text(str(os.getpid()))
    try:
        try:
            from applypilot.notify.digest import check_email_approvals
            approval = check_email_approvals()
            if approval.get("error"):
                console.print(f"[yellow]Approval check failed: {approval['error']}[/yellow]")
                return
            newly = approval["requested"] + approval["approved"]
            if newly:
                console.print(f"[bold blue]New approvals:[/bold blue] {len(newly)} job(s).")
        except Exception as e:
            log.exception("Poll: approval check failed")
            console.print(f"[yellow]Approval check failed: {e}[/yellow]")
            return

        conn = get_connection()
        untailored = [
            r[0] for r in conn.execute(
                "SELECT url FROM jobs WHERE apply_status = 'approved' "
                "AND tailored_resume_path IS NULL"
            ).fetchall()
        ]
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE apply_status = 'approved' "
            "AND tailored_resume_path IS NOT NULL"
        ).fetchone()[0]

        if not untailored and not ready:
            console.print("[dim]Nothing approved. Nothing to do.[/dim]")
            return

        if untailored:
            console.print(f"[bold blue]Tailoring[/bold blue] {len(untailored)} approved job(s)...")
            try:
                from applypilot.scoring.tailor import run_tailoring
                from applypilot.scoring.cover_letter import run_cover_letters
                from applypilot.scoring.pdf import batch_convert
                run_tailoring(job_urls=untailored, validation_mode="normal")
                run_cover_letters(job_urls=untailored, validation_mode="normal")
                batch_convert()
            except Exception as e:
                log.exception("Poll: tailoring failed")
                console.print(f"[yellow]Tailoring failed: {e}[/yellow]")

        if no_apply:
            console.print("[dim]--no-apply: stopping before submission.[/dim]")
            return
        if get_tier() < 3:
            console.print("[dim]Submission skipped -- Tier 3 not available.[/dim]")
            return

        try:
            from applypilot.apply.launcher import main as apply_main
            console.print(f"[bold red]Submitting[/bold red] up to {limit} approved job(s)...")
            # min_score=0: approval is the gate here, not the score. A job you
            # explicitly named must not be silently skipped because a re-score
            # later nudged it below the digest threshold.
            apply_main(limit=limit, min_score=0, headless=True, dry_run=False, workers=1)
        except Exception as e:
            log.exception("Poll: submission failed")
            console.print(f"[yellow]Submission failed: {e}[/yellow]")
    finally:
        poll_lock.unlink(missing_ok=True)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Review mode: fill the form, screenshot it, write a summary, but never click Submit. Sets apply_status='pending_review' -- does NOT apply."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    list_pending: bool = typer.Option(False, "--list-pending", help="List jobs awaiting review (from a --dry-run pass) with their filled-form summaries."),
    approve: Optional[str] = typer.Option(None, "--approve", help="Approve one pending_review job (by URL) for a real submission."),
    approve_all: bool = typer.Option(False, "--approve-all", help="Approve every pending_review job. Read --list-pending first."),
) -> None:
    """Launch auto-apply to submit job applications.

    SAFETY MODEL: a real (live, submitting) run only ever picks up jobs with
    apply_status='approved' -- it is not possible to submit a real
    application that hasn't first been through a --dry-run review pass and
    been explicitly approved via --approve/--approve-all. The workflow is:

        1. applypilot apply --dry-run [--limit N]   (fills forms, never submits, queues for review)
        2. applypilot apply --list-pending           (read what would have been submitted)
        3. applypilot apply --approve <url>          (or --approve-all, once you're satisfied)
        4. applypilot apply [--limit N]               (submits ONLY the approved jobs, for real)
    """
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if list_pending:
        from applypilot.apply.launcher import list_pending_review
        jobs = list_pending_review()
        if not jobs:
            console.print("[dim]No jobs pending review. Run `applypilot apply --dry-run` first.[/dim]")
            return
        for j in jobs:
            console.print(f"\n[bold]{j['title']}[/bold] @ {j.get('company') or j.get('site', 'Unknown')} "
                          f"(score {j.get('fit_score', '?')}/10)")
            console.print(f"[dim]{j['url']}[/dim]")
            console.print(j.get("review_notes") or "[dim](no summary captured)[/dim]")
            console.print("[dim]" + "-" * 60 + "[/dim]")
        console.print(f"\n[bold]{len(jobs)} job(s) pending review.[/bold] "
                       f"Approve with [bold]--approve <url>[/bold] or [bold]--approve-all[/bold].")
        return

    if approve:
        from applypilot.apply.launcher import approve_job
        if approve_job(approve):
            console.print(f"[green]Approved for real submission:[/green] {approve}")
        else:
            console.print(f"[red]No pending_review job matched that URL.[/red] "
                           f"Run [bold]--list-pending[/bold] to see what's waiting.")
        return

    if approve_all:
        from applypilot.apply.launcher import approve_all_pending
        count = approve_all_pending()
        console.print(f"[green]Approved {count} job(s) for real submission.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print(f"\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    # Check 4: a live (submitting) run only ever draws from apply_status='approved'
    # -- warn clearly rather than silently reporting "queue empty" if there's
    # nothing approved yet, since that's the most likely first-time mistake.
    if not dry_run and not url:
        conn = get_connection()
        approved_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE apply_status = 'approved'"
        ).fetchone()[0]
        if approved_count == 0:
            console.print(
                "[yellow]No jobs are approved for submission yet.[/yellow]\n"
                "A live run only submits jobs that have been through review and approval:\n"
                "  1. [bold]applypilot apply --dry-run[/bold]     (fill forms, never submit, queue for review)\n"
                "  2. [bold]applypilot apply --list-pending[/bold] (read the summaries)\n"
                "  3. [bold]applypilot apply --approve-all[/bold]  (or --approve <url> for specific ones)\n"
                "  4. [bold]applypilot apply[/bold]                (submits only what's approved)"
            )
            raise typer.Exit(code=1)

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    if dry_run:
        console.print(f"  Mode:     [yellow]REVIEW (--dry-run)[/yellow] -- fills forms, never submits, queues for your approval")
    else:
        console.print(f"  Mode:     [bold red]LIVE[/bold red] -- will submit real applications for approved jobs only")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY in ~/.applypilot/.env (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
