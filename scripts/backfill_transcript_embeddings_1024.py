"""One-shot backfill: transcript_chunks.embedding_1024 via Bedrock Cohere v4.

UPDATEs rows in place (keyed by id) so the existing 768-dim `embedding`
column — the rollback path and the old-vs-new verification baseline — is
never touched. The embedded input reproduces transcript_embed.embed_transcript
exactly: "<SYMBOL> earnings call <YYYY-MM-DD>: " + full_text[char_start:char_end].

Resumable: only rows WHERE embedding_1024 IS NULL are processed, in id order
(deterministic). Safe to re-run.

Run (kb image, after migration 036):
  docker compose run --rm --no-deps \
    -e EMBEDDING_PROVIDER=bedrock \
    -e BEDROCK_RPM_LIMIT_GLOBAL_COHERE_EMBED_V4_0=18 \
    kb-migrate python -m scripts.backfill_transcript_embeddings_1024
"""
import asyncio
import logging
import time

from sqlalchemy import text

from bharat_terminal.research.db import session_scope
from bharat_terminal.llm.embeddings import embed_texts, EMBEDDING_DIM, EMBEDDING_PROVIDER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_transcript_embeddings")

BATCH = 96  # Cohere Embed v4 max texts/request


async def main() -> None:
    assert EMBEDDING_PROVIDER == "bedrock" and EMBEDDING_DIM == 1024, (
        f"run with EMBEDDING_PROVIDER=bedrock (got provider={EMBEDDING_PROVIDER}, "
        f"dim={EMBEDDING_DIM})")

    async with session_scope() as s:
        total, done = (await s.execute(text(
            "SELECT count(*), count(embedding_1024) FROM transcript_chunks"))).one()
    logger.info("before: %d chunks, %d already have embedding_1024", total, done)

    t0 = time.monotonic()
    processed = 0
    while True:
        async with session_scope() as s:
            rows = (await s.execute(text("""
                SELECT tc.id, tc.symbol, tc.announced_at, tc.char_start, tc.char_end,
                       substring(ct.full_text FROM tc.char_start + 1
                                 FOR tc.char_end - tc.char_start) AS span
                FROM transcript_chunks tc
                JOIN concall_transcripts ct ON ct.id = tc.transcript_id
                WHERE tc.embedding_1024 IS NULL
                ORDER BY tc.id
                LIMIT :n
            """), {"n": BATCH})).all()
        if not rows:
            break

        inputs = [
            f"{r.symbol} earnings call {r.announced_at.date().isoformat()}: {r.span or ''}"
            for r in rows
        ]
        vecs = await embed_texts(inputs, task_type="RETRIEVAL_DOCUMENT")

        async with session_scope() as s:
            for r, v in zip(rows, vecs):
                await s.execute(text(
                    "UPDATE transcript_chunks SET embedding_1024 = (:v)::vector "
                    "WHERE id = :id"),
                    {"v": "[" + ",".join(f"{x:.6f}" for x in v) + "]", "id": r.id})
            await s.commit()

        processed += len(rows)
        rate = processed / max(1.0, time.monotonic() - t0) * 60
        logger.info("backfilled %d/%d (%.0f rows/min)", done + processed, total, rate)

    async with session_scope() as s:
        total2, done2 = (await s.execute(text(
            "SELECT count(*), count(embedding_1024) FROM transcript_chunks"))).one()
    logger.info("after: %d chunks, %d with embedding_1024 (%d newly embedded)",
                total2, done2, processed)
    if done2 != total2:
        raise SystemExit(f"INCOMPLETE: {total2 - done2} chunks still missing embedding_1024")


if __name__ == "__main__":
    asyncio.run(main())
