-- Make the security and audit event tables append-only for the application's own
-- database role, as a second lock behind the triggers from migration 0005.
--
-- The triggers stop UPDATE and DELETE of rows for every role. They do not fire for
-- TRUNCATE, and a role that owns the tables could drop the triggers. So the role the
-- running app and worker log in with must not own these tables and must hold only
-- SELECT and INSERT on them.
--
-- Run once after the migrations, as the database owner (which runs the migrations and
-- keeps ownership). {role} is the app's login role. tests/test_event_store_role.py
-- applies this file to a throwaway role and checks every denied operation.
GRANT SELECT, INSERT ON security_events, audit_events TO {role};
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON security_events, audit_events FROM {role};
