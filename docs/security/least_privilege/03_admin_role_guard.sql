-- PROPOSAL ONLY (would ship as an approval-gated Alembic revision). Run AS rideshare_migrator.
-- Column grants stop the API from UPDATE-ing users.role, but INSERT cannot be limited by value:
-- a stolen API credential could INSERT a brand-new admin row. This trigger rejects any row that
-- becomes 'admin' unless the session's role is (a member of) rideshare_admin_ops.
CREATE FUNCTION users_admin_role_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.role = 'admin'
       AND (TG_OP = 'INSERT' OR OLD.role IS DISTINCT FROM NEW.role)
       AND NOT pg_has_role(current_user, 'rideshare_admin_ops', 'MEMBER') THEN
        RAISE EXCEPTION 'the admin role can only be granted by the operator role'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER users_admin_role_guard BEFORE INSERT OR UPDATE OF role ON users
    FOR EACH ROW EXECUTE FUNCTION users_admin_role_guard();
