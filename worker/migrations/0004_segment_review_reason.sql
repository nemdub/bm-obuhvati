-- Why a coverage segment was flagged for review (comma-separated reason codes, set by
-- the pipeline; the Worker localizes them for display).
ALTER TABLE coverage_segments ADD COLUMN review_reason TEXT;
