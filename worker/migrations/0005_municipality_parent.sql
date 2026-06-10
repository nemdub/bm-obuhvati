-- City-municipality grouping: a member city-municipality points to its representative
-- (the opstina the city's single RIK document mapped to). The UI hides members, since
-- their stations and coverage live under the representative.
ALTER TABLE municipalities ADD COLUMN parent_id TEXT;
