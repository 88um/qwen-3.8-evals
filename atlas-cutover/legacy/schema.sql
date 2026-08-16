-- Atlas monolith: current production schema (excerpt: the tables in scope for
-- the orders/inventory split, plus the neighbors they touch).
-- PostgreSQL 15. Sizes and write rates are in the brief.

CREATE TABLE customers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_ref    TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'standard'
                    CHECK (tier IN ('standard', 'priority', 'enterprise')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku              TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    -- denormalized: total available across warehouses, maintained by trigger
    -- trg_refresh_available on inventory_levels (see below). The storefront
    -- listing pages read this column ~40k times/sec through the app cache.
    cached_available INTEGER NOT NULL DEFAULT 0,
    discontinued     BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE warehouses (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code      TEXT NOT NULL UNIQUE,
    region    TEXT NOT NULL
);

CREATE TABLE inventory_levels (
    product_id   BIGINT NOT NULL REFERENCES products(id),
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved     INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

-- Append-only movement log. Written by every path that touches stock; the
-- nightly reconcile job (code-paths.md §3) treats it as the source of truth.
-- Note: intentionally no primary key; it is written with COPY during warehouse
-- imports and the ORM maps it as a write-only entity.
CREATE TABLE inventory_movements (
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    delta        INTEGER NOT NULL,
    reason       TEXT NOT NULL CHECK (reason IN
                 ('order', 'cancel', 'import', 'adjustment', 'reconcile')),
    order_id     BIGINT,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX inventory_movements_prod_time
    ON inventory_movements (product_id, occurred_at);

-- Order numbers: allocated from this global sequence at INSERT time. Customer
-- support, invoices, and the partner EDI feed all reference order_number, and
-- partners rely on numbers being strictly increasing per customer.
CREATE SEQUENCE order_number_seq START 88000001;

CREATE TABLE orders (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_number   BIGINT NOT NULL UNIQUE DEFAULT nextval('order_number_seq'),
    customer_id    BIGINT NOT NULL REFERENCES customers(id),
    status         TEXT NOT NULL DEFAULT 'placed' CHECK (status IN
                   ('placed', 'paid', 'picking', 'shipped', 'delivered',
                    'canceled')),
    total_cents    INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX orders_customer ON orders (customer_id, placed_at DESC);

CREATE TABLE order_lines (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id   BIGINT NOT NULL REFERENCES products(id),
    warehouse_id BIGINT NOT NULL REFERENCES warehouses(id),
    qty          INTEGER NOT NULL CHECK (qty > 0),
    price_cents  INTEGER NOT NULL
);
CREATE INDEX order_lines_order ON order_lines (order_id);

-- Reservations hold stock between placement and shipment. Rows are DELETEd on
-- cancel (code-paths.md §2) and on shipment confirmation.
CREATE TABLE reservations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id),
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (product_id, warehouse_id)
        REFERENCES inventory_levels (product_id, warehouse_id)
);
CREATE INDEX reservations_order ON reservations (order_id);

-- Status history: append-only, written by an ORM hook on every orders UPDATE.
-- No primary key; queried only by order_id for the support UI timeline.
CREATE TABLE order_status_history (
    order_id    BIGINT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by  TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX osh_order ON order_status_history (order_id, changed_at);

-- Company-wide audit log. Lives in the monolith and stays there; compliance
-- reads it. The trigger below writes to it on every orders change.
CREATE TABLE audit_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name  TEXT NOT NULL,
    row_pk      TEXT NOT NULL,
    action      TEXT NOT NULL,
    diff        JSONB,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trigger 1: keep products.cached_available in sync with inventory_levels.
CREATE OR REPLACE FUNCTION refresh_available() RETURNS trigger AS $$
BEGIN
    UPDATE products
       SET cached_available = (
           SELECT COALESCE(SUM(on_hand - reserved), 0)
             FROM inventory_levels
            WHERE product_id = NEW.product_id)
     WHERE id = NEW.product_id;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_refresh_available
AFTER INSERT OR UPDATE ON inventory_levels
FOR EACH ROW EXECUTE FUNCTION refres_available();  -- sic: typo is in prod too;
-- a 2019 migration created the function under both spellings and the trigger
-- binds to the misspelled one. Fixing it has never been prioritized.

-- Trigger 2: audit every orders mutation into audit_log.
CREATE OR REPLACE FUNCTION audit_orders() RETURNS trigger AS $$
BEGIN
    INSERT INTO audit_log (table_name, row_pk, action, diff)
    VALUES ('orders', NEW.id::text, TG_OP,
            jsonb_build_object('status', NEW.status, 'total', NEW.total_cents));
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_orders
AFTER INSERT OR UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION audit_orders();
