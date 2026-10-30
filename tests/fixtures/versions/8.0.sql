-- Odoo 8.0 shape.
--   * res_users.login_date is a REAL column (written at login).
--   * no res_users_log table (introduced in 9.0).
--   * no presence table yet -> users command uses the 'unknown' branch.
--   * no auth_totp / auth_passkey, no cron failure tracking / progress.
--   * res_groups.category_id links straight to ir_module_category.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base', '8.0.1.3', false, 'installed'),
    ('web',  '8.0.1.0', false, 'installed'),
    ('sale', '8.0.1.0', false, 'installed');

ALTER TABLE res_users ADD COLUMN login_date timestamp;
UPDATE res_users SET login_date = '2024-02-01 10:00:00' WHERE id = 1;
UPDATE res_users SET login_date = '2023-11-15 16:45:00' WHERE id = 2;
-- user 6 never logged in -> login_date stays NULL.

ALTER TABLE res_groups ADD COLUMN category_id integer;
UPDATE res_groups SET category_id = 1;
