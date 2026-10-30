-- Odoo 19.0 shape. Cumulative over 18.0, with the two 19.0 replacements:
--   * res_users_log present, no login_date column.
--   * presence table is mail_presence (bus_presence -> mail_presence).
--   * auth_totp -> totp_secret, auth_passkey -> auth_passkey_key (still present).
--   * ir_cron.failure_count / first_failure_date + ir_cron_progress (still present).
--   * res_groups.category_id -> privilege_id -> res_groups_privilege (new chain);
--     res_groups.category_id is gone.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base',         '19.0.1.3', false, 'installed'),
    ('web',          '19.0.1.0', false, 'installed'),
    ('mail',         '19.0.1.0', false, 'installed'),
    ('auth_totp',    '19.0.1.0', false, 'installed'),
    ('auth_passkey', '19.0.1.0', false, 'installed'),
    ('sale',         '19.0.1.0', false, 'installed');

CREATE TABLE res_users_log (
    id serial PRIMARY KEY,
    create_uid integer,
    create_date timestamp
);
INSERT INTO res_users_log (create_uid, create_date) VALUES
    (1, '2024-02-01 10:00:00'),
    (1, '2024-03-05 08:20:00'),
    (2, '2023-11-15 16:45:00');

-- 19.0: presence moved from bus.presence to mail.presence.
CREATE TABLE mail_presence (
    id serial PRIMARY KEY,
    user_id integer,
    status text
);
INSERT INTO mail_presence (user_id, status) VALUES (1, 'online'), (2, 'offline');

ALTER TABLE res_users ADD COLUMN totp_secret text;
UPDATE res_users SET totp_secret = 'ABCDEFGHIJKLMNOP' WHERE id = 1;
INSERT INTO ir_config_parameter (key, value) VALUES ('auth_totp.policy', 'optional');

CREATE TABLE auth_passkey_key (
    id serial PRIMARY KEY,
    name text,
    create_uid integer
);
INSERT INTO auth_passkey_key (name, create_uid) VALUES ('yubikey', 1);

ALTER TABLE ir_cron ADD COLUMN failure_count integer DEFAULT 0;
ALTER TABLE ir_cron ADD COLUMN first_failure_date timestamp;
CREATE TABLE ir_cron_progress (
    id serial PRIMARY KEY,
    cron_id integer,
    done integer,
    remaining integer,
    timed_out_counter integer
);

-- 19.0: res_groups.category_id replaced by privilege_id -> res_groups_privilege.
CREATE TABLE res_groups_privilege (
    id serial PRIMARY KEY,
    name text,
    category_id integer
);
INSERT INTO res_groups_privilege (id, name, category_id) VALUES (1, 'User types', 1);
ALTER TABLE res_groups ADD COLUMN privilege_id integer;
UPDATE res_groups SET privilege_id = 1;
