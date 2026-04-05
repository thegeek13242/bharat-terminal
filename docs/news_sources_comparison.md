# Indian Financial News Sources — Ranked Comparison

| Rank | Source | Latency | Machine-Readability | Reliability | Licensing | Coverage | Score |
|------|--------|---------|--------------------|-----------|-----------|---------|----|
| 1 | NSE Filings | ~5s | 9/10 (JSON API) | 99.9% | Free/Official | Corporate actions, results | 9.2 |
| 2 | BSE Filings | ~5s | 8/10 (JSON API) | 99.9% | Free/Official | Corporate filings, XBRL | 9.0 |
| 3 | Reuters India | ~30s | 8/10 (RSS) | 99.5% | Free RSS (editorial) | Markets, macro, companies | 8.5 |
| 4 | Economic Times | ~60s | 7/10 (RSS) | 99.0% | Free RSS | Broad market, companies | 8.0 |
| 5 | Mint/LiveMint | ~60s | 7/10 (RSS) | 98.5% | Free RSS | Companies, IPO, sectors | 7.8 |
| 6 | MoneyControl | ~90s | 6/10 (RSS, partial) | 98.0% | Free RSS | Stocks, MF, economy | 7.5 |
| 7 | NDTV Profit | ~120s | 6/10 (RSS) | 97.5% | Free RSS | Markets, economy | 7.2 |
| 8 | PTI | ~120s | 5/10 (via GNews RSS) | 97.0% | Restricted direct, GNews free | Broad economy | 6.8 |

**Latency** = time from event to machine-readable availability
**Reliability** = estimated uptime/availability of feed
**Score** = weighted average (latency 30%, machine-readability 25%, reliability 25%, coverage 20%)

## SLA Design

| Adapter | Poll Interval | P95 Ingest Latency Target | Circuit Breaker Threshold |
|---------|--------------|--------------------------|--------------------------|
| NSE_FILINGS | 30s | ≤200ms | 5 failures / 60s recovery |
| BSE_FILINGS | 30s | ≤200ms | 5 failures / 60s recovery |
| REUTERS_INDIA | 120s | ≤500ms | 5 failures / 60s recovery |
| ECONOMIC_TIMES | 120s | ≤500ms | 3 failures / 120s recovery |
| MINT | 120s | ≤500ms | 3 failures / 120s recovery |
| MONEYCONTROL | 120s | ≤800ms | 3 failures / 120s recovery |
| NDTV_PROFIT | 120s | ≤800ms | 3 failures / 120s recovery |
| PTI | 180s | ≤1000ms | 3 failures / 180s recovery |

## Kafka Topic Design

```
raw.news.in
  partitions: 8 (keyed by source — enables parallel per-source consumption)
  retention: 24h (news items are time-sensitive)
  replication: 1 (dev), 3 (prod)
  consumers: analysis-pipeline (group: analysis-workers, 8 consumers)

raw.news.dlq
  partitions: 2
  retention: 7d
  consumers: ops-monitoring, dead-letter-reprocessor

analysed.impact.in
  partitions: 8
  retention: 7d
  consumers: frontend-ws-server (group: frontend-consumers)
```
