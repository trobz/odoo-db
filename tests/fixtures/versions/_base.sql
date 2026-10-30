-- Shared base schema for every per-version fixture.
--
-- Loaded first, then overlaid by <version>.sql which adds the shapes that
-- distinguish each Odoo version (see tests/fixtures/versions/README.md).
--
-- This is deliberately a *synthetic* subset: just the tables, columns and a
-- handful of rows that odoo-db's shape probes and the shape-sensitive commands
-- actually read. It is NOT a real Odoo schema and proves only that odoo-db
-- agrees with the shape we assert in the manifest for that version.

-- Odoo detection + version (ir_module_module rows are version-specific, so
-- they live in <version>.sql; this only creates the table).
CREATE TABLE ir_module_module (
    id serial PRIMARY KEY,
    name text,
    latest_version text,
    auto_install boolean DEFAULT false,
    state text
);

-- database.* flags + arbitrary config parameters (auth_totp.policy is added by
-- the totp-capable versions).
CREATE TABLE ir_config_parameter (
    id serial PRIMARY KEY,
    key text,
    value text
);
INSERT INTO ir_config_parameter (key, value) VALUES ('database.is_neutralized', 'true');

-- Users / partners / companies. res_users.login_date and totp_secret are
-- version-specific columns added by <version>.sql.
CREATE TABLE res_partner (
    id serial PRIMARY KEY,
    name text
);
INSERT INTO res_partner (id, name) VALUES
    (1, 'Mitchell Admin'),
    (2, 'Demo User'),
    (3, 'Portal User');

CREATE TABLE res_company (
    id serial PRIMARY KEY,
    name text
);
INSERT INTO res_company (id, name) VALUES (1, 'My Company');

CREATE TABLE res_users (
    id serial PRIMARY KEY,
    login text,
    active boolean DEFAULT true,
    partner_id integer,
    share boolean DEFAULT false,
    create_date timestamp
);
-- create_date years (2019/2021/2023) give get_users_by_year three buckets.
INSERT INTO res_users (id, login, active, partner_id, share, create_date) VALUES
    (1, 'admin',  true, 1, false, '2019-01-05 08:00:00'),
    (2, 'demo',   true, 2, false, '2021-06-10 09:30:00'),
    (6, 'portal', true, 3, true,  '2023-03-20 14:15:00');

-- Groups. res_groups.category_id (<= 18) vs privilege_id -> res_groups_privilege
-- (19) is set up per version; here we only create the base tables both share.
CREATE TABLE ir_module_category (
    id serial PRIMARY KEY,
    name text
);
INSERT INTO ir_module_category (id, name) VALUES (1, 'Administration');

CREATE TABLE res_groups (
    id serial PRIMARY KEY,
    name text
);
INSERT INTO res_groups (id, name) VALUES (1, 'Settings'), (2, 'Access Rights');

-- Crons. ir_cron.failure_count / first_failure_date (18+) are added per version.
CREATE TABLE ir_act_server (
    id serial PRIMARY KEY,
    name text,
    state text,
    code text
);
INSERT INTO ir_act_server (id, name, state, code) VALUES
    (1, 'Mail: Email Queue Manager', 'code', 'model.process_email_queue()');

CREATE TABLE ir_cron (
    id serial PRIMARY KEY,
    cron_name text,
    interval_number integer,
    interval_type text,
    nextcall timestamp,
    ir_actions_server_id integer,
    active boolean DEFAULT true
);
INSERT INTO ir_cron (id, cron_name, interval_number, interval_type, nextcall, ir_actions_server_id, active) VALUES
    (1, 'Mail: Email Queue Manager', 1, 'minutes', now() + interval '1 hour', 1, true);

CREATE TABLE ir_model_data (
    id serial PRIMARY KEY,
    model text,
    res_id integer,
    module text,
    name text
);
INSERT INTO ir_model_data (model, res_id, module, name) VALUES
    ('ir.cron', 1, 'mail', 'ir_cron_mail_queue');
