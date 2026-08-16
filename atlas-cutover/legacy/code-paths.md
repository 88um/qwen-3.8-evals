# Atlas monolith — code paths in scope (as running in production today)

Excerpted and lightly simplified from the application (Python + SQLAlchemy Core;
each function runs inside one database transaction unless noted). These paths are
live at the rates given in the brief. Your plan is graded against these, as they
are — including their warts.

## §1 place_order — the hot path (~55% of the 4,000 writes/sec)

```python
def place_order(customer_id, lines):        # lines: [(product_id, warehouse_id, qty)]
    with db.begin() as tx:                  # ONE transaction, all statements
        total = 0
        for (product_id, warehouse_id, qty) in lines:
            row = tx.execute(
                "SELECT on_hand, reserved FROM inventory_levels "
                "WHERE product_id = %s AND warehouse_id = %s FOR UPDATE",
                (product_id, warehouse_id)).one()
            if row.on_hand - row.reserved < qty:
                raise OutOfStock(product_id)
            tx.execute(
                "UPDATE inventory_levels SET reserved = reserved + %s, "
                "updated_at = now() WHERE product_id = %s AND warehouse_id = %s",
                (qty, product_id, warehouse_id))
            price = tx.execute("SELECT unit_price_cents FROM products "
                               "WHERE id = %s", (product_id,)).one().unit_price_cents
            total += price * qty

        order = tx.execute(
            "INSERT INTO orders (customer_id, total_cents) VALUES (%s, %s) "
            "RETURNING id, order_number", (customer_id, total)).one()

        for (product_id, warehouse_id, qty) in lines:
            tx.execute("INSERT INTO order_lines (order_id, product_id, "
                       "warehouse_id, qty, price_cents) VALUES (%s,%s,%s,%s,%s)",
                       (order.id, product_id, warehouse_id, qty, price))
            tx.execute("INSERT INTO reservations (order_id, product_id, "
                       "warehouse_id, qty) VALUES (%s,%s,%s,%s)",
                       (order.id, product_id, warehouse_id, qty))
            tx.execute("INSERT INTO inventory_movements (product_id, "
                       "warehouse_id, delta, reason, order_id) "
                       "VALUES (%s,%s,%s,'order',%s)",
                       (product_id, warehouse_id, -qty, order.id))
    return order.order_number
```

The no-oversell promise is enforced by the `FOR UPDATE` + conditional check +
`reserved <= on_hand` CHECK, all inside the single transaction that also creates
the order. Order rows and inventory mutations commit **atomically or not at all**
— the storefront's error handling depends on that (a failed placement leaves no
trace, so there is no cleanup path in the application).

## §2 cancel_order — runs ~4,000×/day, support-initiated or customer-initiated

```python
def cancel_order(order_id):
    with db.begin() as tx:
        updated = tx.execute(
            "UPDATE orders SET status = 'canceled', updated_at = now() "
            "WHERE id = %s AND status IN ('placed', 'paid') RETURNING id",
            (order_id,)).rowcount
        if not updated:
            raise NotCancelable(order_id)
        for r in tx.execute("SELECT product_id, warehouse_id, qty "
                            "FROM reservations WHERE order_id = %s", (order_id,)):
            tx.execute("UPDATE inventory_levels SET reserved = reserved - %s "
                       "WHERE product_id = %s AND warehouse_id = %s",
                       (r.qty, r.product_id, r.warehouse_id))
            tx.execute("INSERT INTO inventory_movements (product_id, "
                       "warehouse_id, delta, reason, order_id) "
                       "VALUES (%s,%s,%s,'cancel',%s)",
                       (r.product_id, r.warehouse_id, r.qty, order_id))
        tx.execute("DELETE FROM reservations WHERE order_id = %s", (order_id,))
```

## §3 nightly_reconcile — cron, 02:30 UTC, runs 20–40 minutes

Rebuilds `inventory_levels.on_hand` from the movement log and physical-count
adjustments, in 5,000-row batches:

```python
def nightly_reconcile():
    for batch in product_warehouse_pairs(batch_size=5000):
        with db.begin() as tx:                     # one tx per batch
            tx.execute("""
                UPDATE inventory_levels il
                   SET on_hand = m.total, updated_at = now()
                  FROM (SELECT product_id, warehouse_id,
                               GREATEST(SUM(delta), 0) AS total
                          FROM inventory_movements
                         WHERE (product_id, warehouse_id) IN %(batch)s
                         GROUP BY 1, 2) m
                 WHERE il.product_id = m.product_id
                   AND il.warehouse_id = m.warehouse_id
                   AND il.on_hand IS DISTINCT FROM m.total""",
                {"batch": batch})
```

On a typical night this rewrites 2–6 million `inventory_levels` rows (every row
whose computed total differs, plus the trigger fan-out to `products`).

## §4 warehouse_import — 3× daily per warehouse, bulk stock intake

```python
def warehouse_import(warehouse_id, csv_stream):
    with db.begin() as tx:
        tx.copy_expert(
            "COPY inventory_movements (product_id, warehouse_id, delta, reason) "
            "FROM STDIN WITH CSV", csv_stream)          # 50k-400k rows per file
        tx.execute("""
            INSERT INTO inventory_levels (product_id, warehouse_id, on_hand, reserved)
            SELECT product_id, warehouse_id, GREATEST(SUM(delta), 0), 0
              FROM pg_temp.staged_movements          -- staged during COPY
             GROUP BY 1, 2
            ON CONFLICT (product_id, warehouse_id)
            DO UPDATE SET on_hand = inventory_levels.on_hand + EXCLUDED.on_hand,
                          updated_at = now()""")
```

## §5 EDI partner feed — hourly export consumed by 9 external partners

Reads `orders` joined to `order_lines` **by `order_number` range**: each run
exports `WHERE order_number > last_exported` and advances a stored high-water
mark. Partners deduplicate on `order_number` and reject files whose numbers are
not strictly increasing. This is the consumer that makes the order-number
allocation mechanism (see `schema.sql`) load-bearing beyond the UI.
