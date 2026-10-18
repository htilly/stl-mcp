-- ADMIN_EMAILS makes an account admin once per email, not on every sign-in: a grant is recorded
-- here, so demoting that user later sticks. No foreign key: the record must outlive a deleted
-- account, or a recycled address would be promoted again.

CREATE TABLE admin_grants (
    email      text PRIMARY KEY,
    user_id    text NOT NULL,
    granted_at double precision NOT NULL
);
