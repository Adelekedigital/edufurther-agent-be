"""Submit a batch of the product's discoveries for processing.

The pilot's driver. Stage 1 of the rollout is "process 20-50 records
without changing publication state", and this is how those records are
chosen and handed to the runtime.

Deliberately submits rather than executes: each discovery becomes an agent
job, and the service's own worker and sweeper run them. That means a batch
interrupted halfway leaves durable, resumable jobs rather than losing the
work, and the same command run twice does not process anything twice -
submission is idempotent per (use case, workflow version, discovery).

    uv run python scripts/batch.py --limit 20
    uv run python scripts/batch.py --limit 20 --dry-run

`--dry-run` lists what would be submitted and writes nothing at all.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app.core.eventloop import install_selector_event_loop_policy  # noqa: E402

install_selector_event_loop_policy()

from app.core.config import get_settings  # noqa: E402
from app.core.identifiers import job_idempotency_key, new_correlation_id  # noqa: E402
from app.infra.database import get_sessionmaker  # noqa: E402
from app.infra.jobs import enqueue_job  # noqa: E402
from app.integrations.scholarship_finder import client_from_settings  # noqa: E402
from app.usecases.scholarship_finder.graph import USE_CASE_ID  # noqa: E402

PRODUCT_ID = "scholarship_finder"


async def run(limit: int, *, dry_run: bool) -> int:
    settings = get_settings()
    client = client_from_settings()

    page = await client.list_discoveries(
        limit=limit,
        workflow_version=settings.workflow_version,
        unprocessed_only=True,
    )
    items = page.get("items") or []
    if not items:
        print("Nothing to process: every discovery has a run for this workflow version.")
        print("Bump WORKFLOW_VERSION to deliberately reprocess.")
        return 0

    print(f"workflow version : {settings.workflow_version}")
    print(f"shadow mode      : {settings.shadow_mode}")
    print(f"discoveries      : {len(items)}")
    print()

    if dry_run:
        for item in items:
            print(f"  would submit {item['discovery_id']}  {item.get('raw_title') or '(untitled)'}")
        print("\nDry run: nothing was submitted.")
        return 0

    submitted = existing = 0
    sessions = get_sessionmaker()
    for item in items:
        reference = str(item["discovery_id"])
        async with sessions() as db:
            job, created = await enqueue_job(
                db,
                product_id=PRODUCT_ID,
                use_case_id=USE_CASE_ID,
                input_reference=reference,
                correlation_id=new_correlation_id(),
                idempotency_key=job_idempotency_key(
                    use_case=USE_CASE_ID,
                    workflow_version=settings.workflow_version,
                    input_reference=reference,
                ),
                workflow_version=settings.workflow_version,
                payload={"source_url": item.get("source_url")},
            )
            job_id = job.job_id
            await db.commit()
        if created:
            submitted += 1
            print(f"  submitted {job_id}  <- {reference}")
        else:
            existing += 1
            print(f"  already queued {job_id}  <- {reference}")

    print(f"\nSubmitted {submitted}, already present {existing}.")
    print("The service's worker and sweeper will run them. Watch:")
    print("  GET /api/v1/internal/agent/jobs/{job_id}")
    print("  GET /api/v1/internal/agent/metrics")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, help="how many to submit (max 200)")
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would be submitted, write nothing"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 200:
        parser.error("--limit must be between 1 and 200")

    if os.environ.get("SHADOW_MODE", "").lower() in {"false", "0"}:
        # Not a confirmation prompt - this runs unattended - but the one
        # setting that decides whether a batch touches the product's queue
        # should never be discovered after the fact.
        print("WARNING: SHADOW_MODE is off. This batch will write to the product.\n")

    return asyncio.run(run(args.limit, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
