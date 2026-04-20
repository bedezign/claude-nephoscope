-- v9: permission_rejected — durable record of shapes the user has declined
-- to promote. Prevents the reviewer from re-proposing the same shape as
-- observations accumulate after an `n` in review.sh.
--
-- The learner's scan skips candidate upserts when a shape is rejected;
-- propose_promotions also LEFT JOINs this table to exclude rejected rows
-- as a belt-and-braces guard. The hook is unaffected — rejected shapes
-- simply fall through to the normal permission prompt, which is the
-- default for any shape not in permission_active.
CREATE TABLE permission_rejected (
  command_shape_id INTEGER PRIMARY KEY REFERENCES command_shapes(id),
  rejected_at      TEXT    NOT NULL,
  reason           TEXT
);
CREATE INDEX idx_permission_rejected_rejected_at
  ON permission_rejected(rejected_at);
