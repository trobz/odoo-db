-- Odoo 9.0 shape. The first version where the 8.0 -> 9.0 shift happened:
--   * res_users_log is NOW present (new in 9.0); last login = newest row/user.
--   * res.users.login_date became a non-stored related field -> NO column
--     (verified in 9.0/openerp/addons/base/res/res_users.py:
--      login_date = fields.Datetime(related='log_ids.create_date')).
--   * bus_presence is the presence table (bus module, 9.0/addons/bus).
--   * no auth_totp / auth_passkey, no cron failure tracking / progress.
--   * res_groups.category_id links straight to ir_module_category.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base', '9.0.1.3', false, 'installed'),
    ('web',  '9.0.1.0', false, 'installed'),
    ('bus',  '9.0.1.0', false, 'installed'),
    ('mail', '9.0.1.0', false, 'installed'),
    ('sale', '9.0.1.0', false, 'installed');

CREATE TABLE res_users_log (
    id serial PRIMARY KEY,
    create_uid integer,
    create_date timestamp
);
INSERT INTO res_users_log (create_uid, create_date) VALUES
    (1, '2024-02-01 10:00:00'),
    (1, '2024-03-05 08:20:00'),
    (2, '2023-11-15 16:45:00');
-- user 6 has no res_users_log row -> counted as "never".

CREATE TABLE bus_presence (
    id serial PRIMARY KEY,
    user_id integer,
    status text
);
INSERT INTO bus_presence (user_id, status) VALUES (1, 'online'), (2, 'offline');

ALTER TABLE res_groups ADD COLUMN category_id integer;
UPDATE res_groups SET category_id = 1;
