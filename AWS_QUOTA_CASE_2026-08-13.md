# AWS Service Quotas case — Bedrock Claude RPM increase (draft, 2026-08-13)

Draft text + evidence for a Service Quotas increase request. Filing is a
founder action (AWS console → Service Quotas → Amazon Bedrock, or re-open the
2026-08-12 case). The Haiku-only case was rejected once; this draft asks for
the model family we actually run and carries measured demand evidence.

## What to request

Per-model "On-demand InvokeModel requests per minute" (cross-region / global
profile, ap-south-1 home region) — current applied value 10 RPM per model:

| Model | Current | Requested | Basis |
|---|---|---|---|
| anthropic.claude-haiku-4-5 (global profile) | 10 | **60** | measured peak demand 33/min, p99 19/min |
| anthropic.claude-sonnet-4-6 (global profile) | 10 | **40** | measured peak 16/min, p99 ~10/min |
| anthropic.claude-sonnet-4-5 (global profile) | 10 | **20** | measured peak 8/min |

Headroom sizing: peak-so-far ×~2. Indian results season (quarterly, next
peak mid-Oct) multiplies filing bursts; the Aug measurements are off-season.

## Evidence (from llm_call_log, Bedrock cutover 2026-08-12 → today)

Peak per-minute COMPLETED call rates (post-pacing — raw demand is higher, the
client-side pacer queues calls to stay under the clamp; see
`bharat_terminal/llm/wrapper.py` "Client-side RPM pacing"):

```
           model            | peak_rpm | p99_rpm | active_minutes
----------------------------+----------+---------+----------------
 claude-haiku-4-5-20251001  |       33 |    19.0 |            739
 claude-sonnet-4-6          |       16 |     9.7 |            729
 claude-sonnet-4-5-20250929 |        8 |     6.0 |            637
```

- On 2026-08-12 (cutover day, before pacer budgets were tuned), 37–46% of
  Haiku calls terminally failed on ThrottlingException even after the SDK's
  5-retry ladder — visible in llm_call_log error rates for that day.
- Business context for the case: real-time analysis of Indian exchange
  filings; each filing needs 3 concurrent model calls; bursts are driven by
  exchange disclosure deadlines (results days cluster hundreds of filings in
  minutes). The 10 RPM default forces client-side queueing that directly
  delays market-hours output.

## If AWS declines again — fallback options (in order)

1. **Bedrock mantle endpoint** (`BEDROCK_CLAUDE_ENDPOINT=mantle`,
   us-east-1): separate quota pool, no RPM caps, cached reads free against
   input TPM — but adds ~200ms cross-region RTT per call and needs
   `bedrock-mantle:*` IAM added to the instance role. Wrapper already
   supports it; this is a config flip + IAM change.
2. **Anthropic direct API** for the analysis hot path only (tier-based RPM,
   no per-model clamp); keep Bedrock for everything else. Needs
   ANTHROPIC_API_KEY funding decision.
3. Spread stages across profile families (global/us) — separate per-profile
   quotas; verify the us.* profile family serves the needed model ids first.

## Notes

- The client-side pacer budgets in docker-compose (BEDROCK_RPM_LIMIT*) must
  be raised in step when the quota lands — they are the binding constraint
  today and exist only to convert 429 failures into queueing.
- Prompt caching (enabled in the working tree 2026-08-13) reduces input TPM,
  not RPM — it does not relieve this limit.
