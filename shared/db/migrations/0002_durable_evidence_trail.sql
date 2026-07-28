-- 0002_durable_evidence_trail.sql
--
-- "Make the pipeline's evidence trail durable" (feat/durable-evidence-trail).
--
-- Eleven of fourteen satellite fields added during the recent hardening
-- effort lived only in memory for a single graph run — GET /results could
-- not see them, and no past run could be audited. Satellite's own
-- confidence had no DB column at all. This migration adds durable columns
-- (for anything filtered/sorted/aggregated on) plus JSONB diagnostics
-- columns (for detail that is read but not queried) to satellite_results,
-- hazard_zones and impact_data.
--
-- Idempotent: every statement uses IF NOT EXISTS so this file is safe to
-- run against a DB that already has some of these columns (e.g. a repeat
-- run, or a DB where 0001 already added scene_age_days).
--
-- Additive only. No existing column is renamed or retyped.

-- ============================================================================
-- satellite_results
-- ============================================================================
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS confidence REAL;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS confidence_basis TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS evidence_count INTEGER;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS coverage_percent REAL;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS coverage_status TEXT;
-- scene_age_days already added in 0001_baseline_drift.sql (it was confirmed
-- ABSENT from live Neon entirely, not merely unwritten — see that file and
-- root CLAUDE.md). Re-stated here with IF NOT EXISTS for idempotency /
-- readability of this migration as a self-contained diff of the durable-
-- evidence-trail feature.
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS scene_age_days INTEGER;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS index_calibrated BOOLEAN;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS index_units TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS selection_reason TEXT;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS scene_cloud_percent REAL;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS aoi_cloud_percent REAL;
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS scl_reused BOOLEAN;
-- diagnostics carries: concerns, validations, needs_verification,
-- should_alert, artifacts_incomplete, failed_artifacts, gap_count,
-- gap_area_km2, gap_attribution, gap_limited_by, gap geometry,
-- coverage_tier, temporal_spread_days, acquisition_count, bytes_downloaded,
-- processing_level.
ALTER TABLE satellite_results ADD COLUMN IF NOT EXISTS diagnostics JSONB;

-- ============================================================================
-- hazard_zones
-- ============================================================================
-- diagnostics carries: satellite_confidence, confidence_cap_applied,
-- primary_hazard_risk (these three were confirmed NOT reaching
-- confirmed_by today — see agents/hazard/test_field_survival.py and
-- agents/satellite/tests/TESTING_GAP_AUDIT.md), plus the raw third-party
-- evidence trace per hazard type (dem_query/dem_samples/dem_response_raw/
-- slope_computation/threshold_applied/fallback_used for landslide;
-- usgs_query/events_returned/max_magnitude/magnitude_type/threshold_applied
-- for earthquake; the deterministic-fallback branch/index value/type/
-- calibration/thresholds for flood; gdacs query/status/count/used-or-
-- ignored for all three).
ALTER TABLE hazard_zones ADD COLUMN IF NOT EXISTS diagnostics JSONB;

-- ============================================================================
-- impact_data
-- ============================================================================
-- diagnostics carries: population_confidence, infrastructure_confidence,
-- vulnerability_confidence (the three task-level confidences impact
-- currently computes and discards) plus the real evacuation_routes output
-- (also persisted in the existing evacuation_routes column — diagnostics
-- additionally carries it so a single diagnostics read shows the full
-- provenance alongside the task confidences, without requiring a second
-- column read).
ALTER TABLE impact_data ADD COLUMN IF NOT EXISTS diagnostics JSONB;
