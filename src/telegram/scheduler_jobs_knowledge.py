"""Wiki and knowledge maintenance scheduler jobs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class KnowledgeJobPorts:
    """Runtime dependencies supplied by the scheduler facade."""
    scoped_write: Callable[..., Any]
    _mark_job_success: Callable[..., Any]
    _notify_job_failure: Callable[..., Any]
    with_async_retry: Callable[..., Any]
    t: Callable[..., Any]
    logger: Any


async def run_wiki_curator(context, ports: KnowledgeJobPorts) -> None:
    """Daily job: harvest new DB data, call LLM to curate wiki pages."""
    from config.defaults import WIKI_CURATOR_MAX_JOB_SECONDS

    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from config import defaults
            from config.settings import BASE_DIR, CONFIG
            if defaults.WIKI_PUBLICATION_V2 and not defaults.WIKI_PUBLICATION_DISABLED:
                from src.wiki.publication.worker import run_publication_maintenance
                result = await asyncio.to_thread(
                    run_publication_maintenance, wiki_dir=BASE_DIR / "wiki", max_jobs=10,
                )
                ports.logger.info(
                    "Wiki publication v2: discovered=%d proposed=%d completed=%d failed=%d manual=%d dead=%d",
                    result.discovered, result.proposed, result.completed, result.failed,
                    result.manual_review, result.dead_letter,
                )
                return

            from src.wiki.compiler import run_nightly_wiki_compiler
            result = await asyncio.to_thread(run_nightly_wiki_compiler, None)
            pages = result.get("pages", [])
            if pages:
                ports.logger.info(
                    "Wiki curator: %d created, %d updated from %d items",
                    result["created"], result["updated"], result["harvested"],
                )
                if CONFIG.wiki_curator_auto_publish:
                    lines = [ports.t("sched.wiki_updated").format(count=result['harvested'])]
                else:
                    lines = [f"📋 Wiki curator proposed {result['harvested']} items (pending approval):"]
                created_pages = [p for p in pages if p["action"] == "created"]
                updated_pages = [p for p in pages if p["action"] == "updated"]
                if created_pages:
                    lines.append(f"\n{ports.t('sched.wiki_new_pages').format(count=len(created_pages))}")
                    for p in created_pages[:5]:
                        lines.append(f"  • `{p['path']}` — {p['title']}")
                    if len(created_pages) > 5:
                        lines.append(ports.t("sched.wiki_more").format(count=len(created_pages) - 5))
                if updated_pages:
                    lines.append(f"\n{ports.t('sched.wiki_updated_pages').format(count=len(updated_pages))}")
                    for p in updated_pages[:5]:
                        lines.append(f"  • `{p['path']}` — {p['title']}")
                    if len(updated_pages) > 5:
                        lines.append(ports.t("sched.wiki_more").format(count=len(updated_pages) - 5))
                if CONFIG.wiki_curator_auto_publish:
                    lines.append("\n_Use: 「改 markets/jp\\_market 嗰條 odd-lot」to edit any page._")
                else:
                    lines.append("\n_Use /wiki\\_approve or Dashboard to publish._")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                )

        await asyncio.wait_for(
            ports.with_async_retry(_core, max_retries=1, delay_seconds=120, job_name="wiki_curator"),
            timeout=WIKI_CURATOR_MAX_JOB_SECONDS,
        )
        ports._mark_job_success("wiki_curator")
    except asyncio.TimeoutError:
        ports.logger.warning("Wiki curator job hit %ds timeout — partial progress saved", WIKI_CURATOR_MAX_JOB_SECONDS)
        await ports._notify_job_failure(context, chat_id, "wiki_curator", TimeoutError(f"Job exceeded {WIKI_CURATOR_MAX_JOB_SECONDS}s timeout"))
    except Exception as exc:
        ports.logger.exception("Wiki curator job failed")
        await ports._notify_job_failure(context, chat_id, "wiki_curator", exc)


async def run_wiki_lint(context, ports: KnowledgeJobPorts) -> None:
    """Weekly: rebuild index and report lint/corpus health without wiki writes."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from src.wiki.health import run_weekly_health
            from config.settings import BASE_DIR
            wiki_dir = BASE_DIR / "wiki"
            summary = await asyncio.to_thread(run_weekly_health, wiki_dir)

            lines = [ports.t("sched.wiki_lint_title")]
            lines.append(
                f"Active: {summary['active']} · weak sources: {summary['weak_sources']} · "
                f"dead links: {summary['dead_links']} · near-dups: {summary['near_dup_pairs']}"
            )
            if summary.get("market_fp_filtered"):
                lines.append(
                    f"_Markets false-positive pairs filtered: {summary['market_fp_filtered']}_"
                )
            issues = summary["unverified"] + summary["dead_links"]
            if summary["unverified"]:
                lines.append(ports.t("sched.wiki_lint_unverified").format(count=summary["unverified"]))
                lines.append("  See wiki/lint\\_report.md")
            if summary["dead_links"]:
                lines.append(
                    ports.t("sched.wiki_lint_dead_links").format(count=summary["dead_links"])
                    + " — see wiki/lint\\_report.md"
                )
            if summary["weak_sources"]:
                lines.append(
                    f"📝 Weak sources: {summary['weak_sources']} "
                    f"(checklist: docs/scratch/wiki\\_weak\\_sources.md)"
                )
            if summary.get("orphans"):
                lines.append(
                    f"🔗 Semantic orphans: {summary['orphans']} — report only; no automatic links are written."
                )
            if issues or summary["weak_sources"] or summary["near_dup_pairs"] or summary.get("orphans"):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                )

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="wiki_lint")
        ports._mark_job_success("wiki_lint")
    except Exception as exc:
        ports.logger.exception("Wiki lint job failed")
        await ports._notify_job_failure(context, chat_id, "wiki_lint", exc)


async def run_insight_mining(context, ports: KnowledgeJobPorts) -> None:
    """Weekly: mine activity_log for patterns, write insight wiki pages."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from config import defaults
            if defaults.WIKI_PUBLICATION_V2 and not defaults.WIKI_PUBLICATION_DISABLED:
                ports.logger.info("Insight mining Wiki writes disabled by Wiki publication v2")
                return
            from src.learning.insight_mining import mine_and_write_insights
            written = mine_and_write_insights()
            if written:
                ports.logger.info("Insight mining: wrote %d new insight pages", written)

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="insight_mining")
        ports._mark_job_success("insight_mining")
    except Exception as exc:
        ports.logger.exception("Insight mining job failed")
        await ports._notify_job_failure(context, chat_id, "insight_mining", exc)


async def run_email_knowledge_mining(context, ports: KnowledgeJobPorts) -> None:
    """Daily job: mine email threads for domain knowledge."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from src.learning.email_knowledge_miner import mine_email_knowledge
            from src.wiki.indexer import invalidate_wiki_index
            from config.settings import BASE_DIR

            wiki_dir = BASE_DIR / "wiki"
            # conn=None — miner uses read/write per thread; no lock during LLM.
            report = await asyncio.to_thread(mine_email_knowledge, None, wiki_dir)
            if report.wiki_pages_written > 0:
                invalidate_wiki_index()

            if report.entries_created > 0:
                ports.logger.info(
                    "Email knowledge mining: %d threads -> %d entries, %d wiki pages",
                    report.threads_processed, report.entries_created, report.wiki_pages_written,
                )
            # Store report in working_memory
            import json
            from dataclasses import asdict
            with ports.scoped_write() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO projects "
                    "(project_id, project_status, ball_with, operator_id) "
                    "VALUES ('_system', 'Active', 'Internal', 'system')"
                )
                from src.repositories.working_memory_repo import upsert_memory
                upsert_memory(conn, "_system", "email_knowledge_mining_last_run", json.dumps(asdict(report)))
                conn.commit()

            return report

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="email_knowledge_mining")
        ports._mark_job_success("email_knowledge_mining")

    except Exception as exc:
        ports.logger.warning("Email knowledge mining job failed: %s", exc, exc_info=True)
        await ports._notify_job_failure(context, chat_id, "email_knowledge_mining", exc)


async def run_knowledge_curation(context, ports: KnowledgeJobPorts) -> None:
    """Weekly (Sunday 08:00 HKT): promote drafts, archive stale knowledge, backfill domains."""
    data = context.job.data
    chat_id = data["chat_id"]
    try:
        async def _core():
            from src.learning.knowledge_curation import run_knowledge_curation
            report = run_knowledge_curation()
            ports.logger.info(
                "Knowledge curation: promoted=%d archived=%d backfilled=%d",
                report["promoted"], report["archived"], report["backfilled"],
            )
            return report

        await ports.with_async_retry(_core, max_retries=1, delay_seconds=60, job_name="knowledge_curation")
        ports._mark_job_success("knowledge_curation")
    except Exception as exc:
        ports.logger.error("Knowledge curation job failed: %s", exc, exc_info=True)
        await ports._notify_job_failure(context, chat_id, "knowledge_curation", exc)
