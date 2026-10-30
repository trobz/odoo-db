-- Odoo 12.0 shape.
--   * res_users.login_date is a non-stored related field -> NO column.
--   * res_users_log present (last login = newest row per user).
--   * bus_presence is the presence table.
--   * no auth_totp / auth_passkey, no cron failure tracking / progress.
--   * res_groups.category_id links straight to ir_module_category.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base', '12.0.1.3', false, 'installed'),
    ('web',  '12.0.1.0', false, 'installed'),
    ('bus',  '12.0.1.0', false, 'installed'),
    ('mail', '12.0.1.0', false, 'installed'),
    ('sale', '12.0.1.0', false, 'installed');

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
