"""Read-only check: does the 1024-dim space rank like the 768-dim space did?

For a deterministic sample of rows that hold BOTH vectors, use each row as a
query in its own table and compare the top-K neighbor sets produced by the old
(Vertex 768) and new (Cohere 1024) columns. High overlap (recall@K) means the
new space preserves the retrieval structure the memory/transcript features were
validated on; low overlap flags a silent-degradation risk BEFORE readers swap.

Different models never rank identically — the bar here (per plan §C "verify
retrieval quality on a held-out sample") is structural agreement, not identity.
Interpretation guide: >=0.5 mean recall@10 is strong cross-model agreement;
0.3-0.5 is acceptable but worth spot-checking; <0.3 demands investigation.

Run (kb image, read-only):
  docker compose run --rm --no-deps kb-migrate \
    python -m scripts.verify_embedding_migration
"""
import asyncio
import logging

from sqlalchemy import text

from bharat_terminal.research.db import session_scope

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("verify_embedding_migration")

SAMPLE = 100
K = 10
SEED = 0.42  # setseed() argument — deterministic sample


async def check(table: str, id_col: str) -> float:
    async with session_scope() as s:
        await s.execute(text("SELECT setseed(:sd)"), {"sd": SEED})
        sample = (await s.execute(text(f"""
            SELECT {id_col} AS id FROM {table}
            WHERE embedding IS NOT NULL AND embedding_1024 IS NOT NULL
            ORDER BY random() LIMIT :n
        """), {"n": SAMPLE})).all()
        if not sample:
            logger.warning("%s: no rows with both embeddings — nothing to compare", table)
            return float("nan")

        overlaps = []
        for row in sample:
            old = (await s.execute(text(f"""
                SELECT {id_col} FROM {table}
                WHERE embedding IS NOT NULL AND {id_col} != :qid
                ORDER BY embedding <=> (SELECT embedding FROM {table} WHERE {id_col} = :qid)
                LIMIT :k
            """), {"qid": row.id, "k": K})).scalars().all()
            new = (await s.execute(text(f"""
                SELECT {id_col} FROM {table}
                WHERE embedding_1024 IS NOT NULL AND {id_col} != :qid
                ORDER BY embedding_1024 <=> (SELECT embedding_1024 FROM {table} WHERE {id_col} = :qid)
                LIMIT :k
            """), {"qid": row.id, "k": K})).scalars().all()
            if old and new:
                overlaps.append(len(set(old) & set(new)) / K)

    mean = sum(overlaps) / len(overlaps)
    logger.info("%s: mean recall@%d over %d sampled queries = %.3f",
                table, K, len(overlaps), mean)
    return mean


async def main() -> None:
    m = await check("memory_items", "id")
    t = await check("transcript_chunks", "id")
    worst = min(x for x in (m, t) if x == x)  # ignore NaN tables
    if worst < 0.3:
        raise SystemExit(f"FAIL: mean recall@{K} {worst:.3f} < 0.3 — investigate "
                         "before swapping readers")
    logger.info("PASS (worst table %.3f)", worst)


if __name__ == "__main__":
    asyncio.run(main())
