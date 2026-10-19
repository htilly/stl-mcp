-- CPU time per user and day, for the admin's usage view: one row is updated after every build
-- (see jobs.py). cpu_seconds is the build thread plus the worker processes it ran (user + system
-- time, all cores together); wall_seconds is how long the builds ran.

CREATE TABLE cpu_usage (
    user_id      text NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    day          date NOT NULL,
    builds       integer NOT NULL DEFAULT 0,
    cpu_seconds  double precision NOT NULL DEFAULT 0,
    wall_seconds double precision NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
