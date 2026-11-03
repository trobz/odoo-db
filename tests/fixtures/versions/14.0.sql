-- Odoo 14.0 shape. Like 12.0/13.0 but auth_totp ships from 14.0 (addons/auth_totp
-- exists on 14.0/15.0 upstream, not 13.0) — this is where the totp + 25k-round
-- passlib combination check-passwords cares about first appears:
--   * res_users_log present, no login_date column.
--   * bus_presence is the presence table.
--   * auth_totp installed -> res_users.totp_secret column present.
--   * no auth_passkey, no cron failure tracking / progress.
--   * res_groups.category_id links straight to ir_module_category.

INSERT INTO ir_module_module (name, latest_version, auto_install, state) VALUES
    ('base', '14.0.1.3', false, 'installed'),
    ('web',  '14.0.1.0', false, 'installed'),
    ('bus',  '14.0.1.0', false, 'installed'),
    ('mail', '14.0.1.0', false, 'installed'),
    ('auth_totp', '14.0.1.0', false, 'installed'),
    ('sale', '14.0.1.0', false, 'installed');

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

-- auth_totp ships from 14.0 (addons/auth_totp). Names stay text here — the
-- jsonb translated-field storage only arrives in 16.0.
ALTER TABLE res_users ADD COLUMN totp_secret text;
UPDATE res_users SET totp_secret = 'ABCDEFGHIJKLMNOP' WHERE id = 1;
INSERT INTO ir_config_parameter (key, value) VALUES ('auth_totp.policy', 'optional');

ALTER TABLE res_groups ADD COLUMN category_id integer;
UPDATE res_groups SET category_id = 1;
