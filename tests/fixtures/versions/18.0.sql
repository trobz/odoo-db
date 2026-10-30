-- Odoo 18.0 shape. Builds on 17.0 and adds the 18.0-only shapes:
--   * res_users_log present, no login_date column.
--   * bus_presence is the presence table.
--   * auth_totp installed -> res_users.totp_secret column present.
--   * auth_passkey installed -> auth_passkey_key table present.
--   * ir_cron.failure_count / first_failure_date columns present.
--   * ir_cron_progress table present.
--   * res_groups.category_id links straight to ir_module_category.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base',         '18.0.1.3', false, 'installed'),
    ('web',          '18.0.1.0', false, 'installed'),
    ('bus',          '18.0.1.0', false, 'installed'),
    ('mail',         '18.0.1.0', false, 'installed'),
    ('auth_totp',    '18.0.1.0', false, 'installed'),
    ('auth_passkey', '18.0.1.0', false, 'installed'),
    ('sale',         '18.0.1.0', false, 'installed');

CREATE TABLE res_users_log (
    id serial PRIMARY KEY,
    create_uid integer,
    create_date timestamp
);
INSERT INTO res_users_log (create_uid, create_date) VALUES
    (1, '2024-02-01 10:00:00'),
    (1, '2024-03-05 08:20:00'),
    (2, '2023-11-15 16:45:00');

CREATE TABLE bus_presence (
    id serial PRIMARY KEY,
    user_id integer,
    status text
);
INSERT INTO bus_presence (user_id, status) VALUES (1, 'online'), (2, 'offline');

ALTER TABLE res_users ADD COLUMN totp_secret text;
UPDATE res_users SET totp_secret = 'ABCDEFGHIJKLMNOP' WHERE id = 1;
INSERT INTO ir_config_parameter (key, value) VALUES ('auth_totp.policy', 'optional');

-- auth_passkey: one registered passkey for admin.
CREATE TABLE auth_passkey_key (
    id serial PRIMARY KEY,
    name text,
    create_uid integer
);
INSERT INTO auth_passkey_key (name, create_uid) VALUES ('yubikey', 1);

-- 18.0 cron failure tracking + progress.
ALTER TABLE ir_cron ADD COLUMN failure_count integer DEFAULT 0;
ALTER TABLE ir_cron ADD COLUMN first_failure_date timestamp;
CREATE TABLE ir_cron_progress (
    id serial PRIMARY KEY,
    cron_id integer,
    done integer,
    remaining integer,
    timed_out_counter integer
);

ALTER TABLE res_groups ADD COLUMN category_id integer;
UPDATE res_groups SET category_id = 1;
