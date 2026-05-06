"""
Bharat Terminal — sentiment subsystem.

Consumes raw Polymarket probability observations from `raw.sentiment.in`,
classifies each market against an India-equity macro taxonomy, and publishes
classified `SentimentSnapshot`s to `analysed.sentiment.in` for downstream
consumption by the API gateway / frontend.

The sentiment-worker is intentionally separate from the news analysis-worker:
    • different cadence (5-min Polymarket cycles vs event-driven news)
    • different shape (probability deltas, not headlines)
    • different model needs (deterministic taxonomy + light LLM, not 5-stage)
"""
