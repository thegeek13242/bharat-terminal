# Commodity shock detector — firing-condition research — 2026-07-15

**Question asked:** crude has risen for days with visible spikes, yet the engine did not
fire. Is a multi-horizon z-score the fix? And: find a **generalized** condition for every
commodity, not a crude-specific tune.

**Answer, in one line:** the fix is **not** a multi-horizon z-score — a z-score would still
miss the current move. The evidence supports **`|1-day move| ≥ k · σ_longrun(commodity)`,
k≈2**, gated by a **per-commodity transmission check**, because 2 of 6 commodities have no
measurable downstream edge at all.

---

## 1. Why the engine is silent (structural, not tuning)

Current design (`bharat_terminal/commodity/detector.py`, 209 lines):

| Parameter | Value |
|---|---|
| detection window | 10 minutes |
| `pct_threshold` | 1.2% (absolute floor) |
| `z_threshold` | 3.0 (vs 60-min baseline vol) |
| `cooldown` | 300 s per commodity |
| firing logic | `|ret| ≥ 1.2%` **AND** `|z| ≥ 3.0` |

Two blind spots follow directly from this design:

**(a) Slow drift is excluded on purpose.** The module docstring states the pct floor
"kills slow drift". A +13% move spread over three sessions never places 1.2% inside any
single 10-minute window. It is invisible by construction.

**(b) Gaps are invisible.** `_evict_old` retains only
`window + baseline + 60s` ≈ 70 minutes of ticks. Across an overnight or weekend break all
history is evicted, so there is no baseline price to compare the reopen against. The
detector cannot see gap moves.

Live state at time of writing: **1,041,000 ticks processed, 11 shocks total** across
CRUDEOIL, SILVER, GOLD, NATURALGAS, COPPER, ALUMINIUM.

**(c) Nothing is persisted.** There is **no commodity price table** in the database
(only `price_points`, which is equities). The detector is a purely in-memory sliding
window fed from Kafka; state is lost on restart. This is the blocker: any
daily-horizon rule is uncomputable today, and no in-house backtest is possible.

---

## 2. The move the engine missed

| | WTI (CL=F) | Brent (BZ=F) |
|---|---|---|
| 3-session | **+12.88%** | **+13.47%** |
| 5-session | +9.64% | +10.55% |
| 10-session | **+15.99%** | **+18.28%** |
| 2026-07-10 → 07-13 | 71.41 → 78.14 (**+9.4%**) | 76.01 → 83.30 (**+9.6%**) |

The decisive event was **a single-session +9.4% gap over a weekend** — not the drift. It
was missed for reason (b) above: no retained history across the break.

---

## 3. Why a multi-horizon z-score does not fix it

z-scoring the current move against its own trailing 120-session return vol:

| Horizon | Return | z |
|---|---|---|
| 1-session | +1.54% | +0.35 |
| 3-session | **+12.81%** | **+1.74** |
| 5-session | +9.58% | +1.00 |
| 10-session | +15.91% | +1.13 |

**A +12.8% three-day crude move scores 1.74σ.** Crude's own recent volatility inflates the
denominator, so the largest moves self-suppress — the same class of bug as the
2026-07-08 vol-window fix, one level up. For reference, `|z| ≥ 3` fires on only ~2.5–2.8%
of days on every horizon (2y), so z≥3 is a reasonable *rarity* threshold; the move simply
does not reach it. **Any pure-z rule, multi-horizon or not, still misses this move.**

Conclusion: statistical surprise is the wrong target variable. What transmits to Indian
equities is the **absolute size** of the move (OMC margins respond to the crude price, not
to how unusual the move was relative to crude's own chop).

---

## 4. Method — transmission event study

Calibrating on the commodity's own statistics is self-referential. The engine exists to
flag moves that **actually move Indian equities**, so the threshold is calibrated against
downstream reaction.

- **Equity data:** `price_points`, 2,123 sessions, 2017-11-28 → 2026-07-14.
- **Commodity data:** Yahoo daily closes (`CL=F`, `NG=F`, `GC=F`, `SI=F`, `HG=F`, `ALI=F`).
- **Causal ordering:** the commodity daily close (US settlement) lands **after** the Indian
  close, so a move through day D is measured against the Indian reaction on **D+1**. No
  lookahead.
- **Abnormal return:** `stock_ret(D→D+1) − market_ret(D→D+1)`, where the market factor is an
  equal-weight, crude-neutral basket: HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, INFY,
  TCS, WIPRO, HCLTECH, ITC, HINDUNILVR, BHARTIARTL, LT.
- **Direction adjustment:** each downstream name carries an expected sign; the reported
  figure is the mean *expected-direction* abnormal return. Positive = transmission behaved
  as theory predicts. `hit%` = share of events with a positive expected-direction response.
- **Normalisation (§6):** `σ_longrun` = stdev of daily commodity returns over the trailing
  **500 sessions**, computed point-in-time (≥400 observations required).

**Methodology note (a bug found and corrected):** the first pass computed the market factor
but never subtracted it, reporting *raw* rather than abnormal returns. On large commodity
moves — often geopolitical risk-off, where the whole market falls — raw returns are badly
contaminated by market beta, and that version showed a spurious *negative* response in the
largest bucket. All figures below are from the corrected abnormal-return version.

**Baskets** (expected sign to a commodity **rise**):

| Commodity | Negative (headwind) | Positive (tailwind) |
|---|---|---|
| CRUDEOIL | BPCL, IOC, HINDPETRO, ASIANPAINT, BERGEPAINT, INDIGO, MRF, APOLLOTYRE, JKTYRE, PIDILITIND, SUPREMEIND, ASTRAL, FINPIPE | ONGC, OIL |
| NATURALGAS | IGL, MGL, GUJGASLTD, CHAMBLFERT, GNFC, RCF, DEEPAKFERT | ONGC, OIL |
| COPPER | POLYCAB, KEI, HAVELLS, FINCABLES | HINDCOPPER, VEDL, HINDALCO |
| ALUMINIUM | — | HINDALCO, NATIONALUM, VEDL |
| GOLD | — | MUTHOOTFIN, MANAPPURAM, TITAN, RAJESHEXPO |
| SILVER | — | TITAN, RAJESHEXPO |

---

## 5. Crude: which horizon actually carries the signal

Downstream abnormal return on D+1, by crude move size and horizon:

**1-session horizon — the money horizon**

| Move | n | abnormal | t | hit% |
|---|---|---|---|---|
| 1–2% | 589 | +0.148% | +5.23 | 61.8 |
| 2–3% | 353 | +0.089% | +1.91 | 53.3 |
| **3–5%** | 233 | **+0.312%** | **+5.83** | **67.8** |
| **≥5%** | 116 | **+0.468%** | **+5.00** | **69.0** |

**3-session horizon**

| Move | n | abnormal | t | hit% |
|---|---|---|---|---|
| 2–4% | 643 | +0.156% | +5.00 | 59.6 |
| 4–6% | 310 | +0.125% | +2.59 | 56.1 |
| 6–8% | 139 | +0.176% | +2.52 | 60.4 |
| 8–12% | 91 | +0.239% | +2.72 | 65.9 |
| **≥12%** | 53 | +0.125% | **+0.67** | 62.3 |

**5-session horizon**

| Move | n | abnormal | t | hit% |
|---|---|---|---|---|
| 3–5% | 456 | +0.158% | +4.55 | 57.2 |
| 5–8% | 347 | +0.065% | +1.34 | 52.4 |
| 8–12% | 170 | +0.206% | +3.21 | 59.4 |
| ≥12% | 97 | +0.086% | +0.78 | 56.7 |

**Read:** transmission scales monotonically with the **1-day** move (3–5% → +0.31%, t=5.8;
≥5% → +0.47%, t=5.0, 69% hit). Multi-day drift is weak and noisy — the 3-session ≥12%
bucket, i.e. **exactly today's situation**, is **t=0.67, not significant**; 5-session ≥12%
is t=0.78. Economically this is intuitive: a sharp 1-day move is *news* and transmits; a
slow grind is anticipated and priced in, leaving little to react to on D+1.

This also means the user-visible complaint ("crude rose for days and nothing fired") and
the actual exploitable signal are different events: the drift itself carries little edge;
the **+9.4% single-session gap inside it** was the high-value miss.

---

## 6. Generalized condition — `k · σ_longrun`, across all six commodities

A flat absolute threshold cannot generalize (5% is enormous for gold, routine for natural
gas). Normalising each commodity's 1-day move by **its own trailing 500-session σ** gives a
scale-free trigger with a single global constant `k`.

Downstream abnormal return on D+1, by k = |1-day move| / σ_longrun:

| Commodity | 1–2σ | 2–3σ | 3–4σ | ≥4σ | verdict |
|---|---|---|---|---|---|
| **ALUMINIUM** | +0.691% (t=6.11, 63.5%) n=392 | **+1.135% (t=4.32, 68.9%)** n=90 | +1.036% (t=1.82, 72.2%) n=18 | +1.315% (t=1.95, 85.7%) n=7 | **strongest** |
| **CRUDEOIL** | +0.229% (t=4.57, 61.6%) n=320 | **+0.365% (t=3.07, 64.6%)** n=65 | +0.773% (t=3.33, 82.4%) n=17 | +0.712% (t=2.12, 70.0%) n=20 | **strong** |
| **COPPER** | +0.236% (t=4.58, 59.6%) n=438 | **+0.293% (t=2.22, 61.2%)** n=85 | +0.437% (t=1.70, 73.7%) n=19 | +0.202% (t=0.58) n=8 | **solid** |
| GOLD | +0.138% (t=2.17, 51.8%) n=409 | +0.268% (t=1.90, 56.7%) n=97 | +0.092% (t=0.47) n=26 | +1.050% (t=1.71) n=17 | marginal |
| NATURALGAS | +0.064% (t=1.12, 51.2%) n=426 | +0.237% (t=1.89, 59.4%) n=69 | +0.081% (t=0.27) n=25 | +0.436% (t=0.86, 47.1%) n=17 | **no edge** |
| SILVER | −0.116% (t=−1.48, 43.7%) n=364 | −0.032% (t=−0.22) n=100 | +0.210% (t=0.68) n=34 | −0.386% (t=−1.03, 45.8%) n=24 | **none / inverted** |

### Finding 1 — `k·σ` is the right generalized trigger
For every commodity that transmits at all, response grows monotonically with k, and
**k ≈ 2** is where it becomes decisive (crude 2–3σ t=3.07/64.6%; aluminium t=4.32/68.9%;
copper t=2.22/61.2%). One global constant, adapting per commodity via its own σ.

It also **independently reproduces the crude result**: crude σ ≈ 2.5%, so k=2 ≈ a 5% daily
move — precisely the strongest bucket in the absolute-threshold study (§5). Two different
parameterisations converging on the same boundary is the main reason to trust it.

### Finding 2 — transmission is not universal (the bigger result)
Crude, aluminium and copper transmit reliably. **Natural gas and silver show no usable
edge** (hit rates 46–51%, i.e. coin flips; silver's sign is inverted). Gold is marginal.

This has a direct operational consequence: the engine's **first-ever live fire was
NATURALGAS at z = −3.01** (2026-07-09), and it emitted 25 company impacts. On 8.5 years of
evidence, natural gas does not move its downstream basket. **We were alerting on noise.**

A firing rule therefore needs **two parts**, not one:

1. **Trigger (universal):** `|1-day move| ≥ k · σ_longrun(commodity)` — k=2 alert, k=3 high.
   Evaluated **close-to-close** so overnight/weekend gaps are captured.
2. **Transmission gate (per-commodity, empirical):** fan out impact only for commodities
   with a validated downstream beta, re-estimated periodically. This is precisely what the
   scaffolded-but-empty `trigger_betas` table was designed to hold.

---

## 7. Honest limitations

- **The negative results may be basket mis-specification, not absence of linkage.**
  TITAN/RAJESHEXPO are weak silver proxies; IGL/MGL pass gas costs through with a regulated
  lag, and Indian gas pricing is only loosely coupled to Henry Hub (`NG=F`). Silver/natgas
  should not be permanently gated off until their baskets are re-specified.
- **Yahoo proxies ≠ the traded contract.** The engine consumes **MCX** ticks; this study
  used WTI/Henry Hub/COMEX/LME proxies. MCX contracts are INR-denominated, so they embed
  USDINR and carry their own basis. Direction and magnitude should hold, but thresholds
  must be re-derived on MCX series before shipping.
- **Effect sizes are modest.** The best case is ~+0.5–1.1% abnormal at a ~69% hit rate.
  Real and strongly significant, but this is a tilt, not a dramatic call — it should be
  presented as "basket likely underperforms ~0.5% tomorrow", never as a certainty.
- **Baskets are hand-curated and equal-weighted**, with fixed signs and no exposure
  weighting; no sector-beta or USDINR control beyond the market factor.
- **Single global market factor** across 8.5 years; no rolling beta estimation.
- Aluminium's ≥3σ buckets rest on n=18 and n=7 — directionally consistent but thin.

---

## 8. Recommended next steps

1. **Persist commodity prices** (new table + backfill). Blocker for everything: the daily
   horizon needs stored closes, and it lets this calibration be re-run in-house on MCX
   series rather than Yahoo proxies.
2. **Implement `k·σ` daily-horizon detection** with close-to-close gap handling; keep the
   intraday spike detector for real-time same-day shocks but drop the unsatisfiable
   `ret ≥ 1.2% AND z ≥ 3` conjunction.
3. **Populate `trigger_betas`** from this study and gate impact fan-out on it.
4. **Re-specify the silver / natural-gas baskets** before gating them off.
5. Demote z-score to a **context field**, not a gate.

---

## Appendix — reproduction

Study parameters: `LOOKBACK = 120` sessions (§3 z-scores); `σ_longrun` = trailing 500
sessions, ≥400 observations required (§6); buckets as tabulated; reaction window D→D+1;
no sampling or randomness anywhere (fully deterministic — re-running reproduces these
figures exactly, subject to Yahoo revisions).

Equity source: `price_points` (2,123 sessions, 2017-11-28 → 2026-07-14).
Commodity source: `query1.finance.yahoo.com/v8/finance/chart/{CL=F,NG=F,GC=F,SI=F,HG=F,ALI=F}?range=10y&interval=1d`.

Detector under review: `bharat_terminal/commodity/detector.py` @ `09c7623`.
