-- Horizon-spanning research questions for the factory.
--
-- WHY THESE, AND WHY NOW
-- The 2026-09-17 observational pilot ran 11 cycles against a single question
-- (RQ-000001) and produced 24 hypotheses collapsing to 16 distinct mechanics
-- and exactly ONE finding. It was not short of intelligence; it was short of
-- questions. Every feature below was measured on 2026-09-17 and every claim in
-- these questions cites a verified number.
--
-- Horizons map to the DSL's Horizon enum: swing (holding_days <= 20),
-- medium (>= 20), intraday (<= 1, squareoff_intraday, and currently refused by
-- the compiler for want of minute-level history).
--
-- REVIEW BEFORE RUNNING. Idempotent: ON CONFLICT DO NOTHING throughout.
--   docker exec -i bharat-postgres psql -U bharat -d bharat_terminal \
--     < scripts/factory_seed_questions_2026-09-17.sql

BEGIN;

INSERT INTO factory.research_families
  (family_id, title, horizon, data_tier, status, budget_cap_usd) VALUES
 ('expectation_surprise_swing','Fundamental surprise vs the SUE recipe, 1-10 day holds','swing','H','active',200.00),
 ('sector_neutral_pead','Is the PEAD long leg a sector-rotation artifact?','swing','H','active',150.00),
 ('guidance_gap_medium','Management guidance as a quarter-horizon conditioner','medium','H','active',200.00),
 ('promoter_flow_medium','Promoter holding changes as a quarter-horizon signal','medium','H','active',200.00),
 -- Deliberately 'blocked' with a $0 cap: an active family the compiler always
 -- refuses would burn agent turns on an impossible task every cycle.
 ('intraday_reaction','Same-session reaction to filings','intraday','F','blocked',0.00)
ON CONFLICT (family_id) DO NOTHING;

INSERT INTO factory.research_questions
  (question_id, family_id, text, source, status) VALUES

('RQ-000002','expectation_surprise_swing',
 'expectation_surprise_gbm@1.0 is a fundamental surprise computed against the E1 global-GBM expectation, independent of the SUE recipe: 126,800 rows from 2023-04-06, coverage 1.000 (measured 2026-09-17). Ranking on it is a different bet from sue_composite@2.1, not a reparameterisation. At SHORT holds (1, 2, 3, 5, 10 days), does a top-decile expectation_surprise_gbm long leg beat the incumbent top-decile sue_composite long leg net of costs? Falsified if its net abnormal return does not exceed the incumbent at the same hold with overlapping cohort CIs.',
 'founder','open'),

('RQ-000003','sector_neutral_pead',
 'sector_bse@1.0 was measured at 85.0% on 2026-09-17 and is the ONLY sector control above the 80% floor (sector_nse is 18.8%). EXP-000001 found the top-decile SUE long leg nets +1.806% over 1,741 events, but nothing has ever tested whether that survives sector neutralisation. Does the effect persist when sector_bse is added as a control, or is it a sector-rotation artifact concentrated in whichever sectors happened to report well? Falsified if the sector-controlled effect loses more than half its magnitude or its cohort t falls below 2.',
 'founder','open'),

('RQ-000004','guidance_gap_medium',
 'guidance_change@1.0 (47,866 rows, coverage 1.000, from 2024-04-05) records whether management raised, lowered, reiterated or issued new guidance, and expectation_surprise_guidance@1.0 measures the actual against that guidance. Both are point-in-time and neither has ever been used in a hypothesis. Over a QUARTER horizon (20, 40, 60 day holds), does a guidance verdict condition post-announcement drift beyond what the earnings surprise alone explains? Falsified if splitting the top SUE decile by guidance verdict produces no separation in net abnormal return at 40 days.',
 'founder','open'),

('RQ-000005','promoter_flow_medium',
 'shp_promoter_delta@1.0 is the quarter-on-quarter change in promoter holding: coverage 0.969, history from 2011-06-30, the longest of any feature in the registry and entirely unused. Promoter accumulation is a costly insider signal with a natural quarterly clock. Over 40-60 day holds, does a promoter-holding increase predict positive net abnormal returns, and is it independent of the earnings surprise? Falsified if the effect is absent once liquidity_tier is controlled, or if it disappears when restricted to non-results windows.',
 'founder','open'),

('RQ-000006','intraday_reaction',
 'BLOCKED ON DATA, recorded so the gap is explicit rather than forgotten. The DSL already supports Horizon.intraday (squareoff_intraday, holding_days <= 1, intraday entry rules) and the compiler refuses it with: "intraday research needs minute-level marks, which only exist forward (paper_signal_marks from 2026-08-13)". price_points is daily-only. Once intraday bars are available from Angel One, the first question is whether the same-session reaction to a filing predicts the multi-day drift that follows, which would let the entry clock move earlier. Do NOT open this family until the data exists.',
 'founder','blocked')
ON CONFLICT (question_id) DO NOTHING;

COMMIT;

-- Verify:
--   SELECT q.question_id, q.status, f.horizon, f.data_tier, f.family_id
--     FROM factory.research_questions q
--     JOIN factory.research_families f USING (family_id) ORDER BY 1;
