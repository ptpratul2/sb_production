from collections import defaultdict

import frappe
from frappe.utils import cint, flt

RESERVATION_CHUNK_SIZE = 50          # legacy: row-count cap for older code paths
RESERVATION_SERIALS_PER_ROW = 200    # max serials grouped into a single Stock Entry Detail row
RESERVATION_SERIALS_PER_ENTRY = 500  # max total serials per submitted Stock Entry


def _init_reservation_bin_tracker(reusable_stock_entry=None):
    """
    Track how much qty can still be issued from each (item_code, source_warehouse)
    for reservation transfers.

    Uses ERPNext ``get_stock_balance`` (last SLE qty_after_transaction), same basis
    as submit-time negative-stock checks — not ``tabBin.actual_qty``, which can drift.
    Subtracts outward qty already on a draft Stock Entry (not yet in the ledger).
    """
    from erpnext.stock.utils import get_stock_balance

    remaining = {}

    def _key(item_code, warehouse):
        return (item_code, warehouse)

    def _ensure(item_code, warehouse):
        k = _key(item_code, warehouse)
        if k not in remaining:
            remaining[k] = flt(get_stock_balance(item_code, warehouse) or 0)
        return k

    if reusable_stock_entry and reusable_stock_entry.docstatus == 0:
        for row in reusable_stock_entry.get("items") or []:
            sw = row.get("s_warehouse")
            ic = row.get("item_code")
            if not sw or not ic:
                continue
            k = _ensure(ic, sw)
            remaining[k] -= flt(row.get("qty") or 0)

    def try_consume(item_code, warehouse, qty):
        k = _ensure(item_code, warehouse)
        if remaining[k] < flt(qty):
            return False
        remaining[k] -= flt(qty)
        return True

    return try_consume


@frappe.whitelist()
def reserve_stock_physically(fg_selector_name):
    """
    Enqueues the stock reservation process to run in the background.
    """
    job = frappe.enqueue(
        'sb.sb.stock_reserve.reserve_stock_background',
        queue='long',
        timeout=3600,
        fg_selector_name=fg_selector_name,
        user=frappe.session.user
    )
    
    return {
        "status": "queued",
        "message": f"Stock reservation has been queued (Job ID: {job.id}). You will be notified when it completes.",
        "job_id": job.id
    }

def reserve_stock_background(fg_selector_name, user):
    try:
        # Ensure fresh DB connection (long-running context)
        frappe.db.connect()
        
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
        from sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector import (
            _allocate_rm_oc_pieces_for_reservation,
        )

        reserved_warehouse = doc.reserved_warehouse or "Reserve - Trial - Sbs"
        oc_warehouse = doc.offcut_warehouse or "OC - Trial - Sbs"
        rm_warehouse = doc.raw_material_warehouse or "RM - Trial - Sbs"
        company = doc.company or frappe.defaults.get_user_default("company")
        cost_center = doc.cost_center or frappe.defaults.get_user_default("cost_center")

        if not company:
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': 'Company is required.',
                    'indicator': 'red',
                    'alert': True
                },
                user=user
            )
            return

        # Reuse latest draft transfer for same selector instead of creating duplicates.
        existing_open_entry = frappe.db.get_value(
            "Stock Entry",
            {
                "fg_raw_material_selector": fg_selector_name,
                "docstatus": 0,
                "purpose": "Material Transfer",
            },
            "name",
            order_by="creation desc",
        )

        # Reservation-only RM/OC allocation (does NOT write rm_oc_simulation rows)
        serial_nos, piece_map, _ns_shortfalls = _allocate_rm_oc_pieces_for_reservation(doc)

        if not serial_nos:
            # No allocation possible — still persist IS/NIS status changes for UI.
            doc.save(ignore_permissions=True)
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': 'No pieces to reserve.',
                    'indicator': 'orange',
                    'alert': True
                },
                user=user
            )
            return

        # Safety: ensure piece_map warehouses align with doc configuration
        # (We don't use `item_code` here, only `s_warehouse`.)
        expected_oc_wh = oc_warehouse
        expected_rm_wh = rm_warehouse
        for sn, info in list(piece_map.items()):
            if info.get("source_type") == "OC":
                info["s_warehouse"] = expected_oc_wh
            elif info.get("source_type") == "RM":
                info["s_warehouse"] = expected_rm_wh

        # ONE QUERY — works on ALL Frappe v15 versions
        serial_details = frappe.db.sql("""
            SELECT name, item_code, warehouse, custom_length
            FROM `tabSerial No`
            WHERE name IN (%s)
        """ % ', '.join(['%s'] * len(serial_nos)), tuple(serial_nos), as_dict=True)

        # Convert to dict for fast lookup
        serial_dict = {row.name: row for row in serial_details}

        def _new_reservation_entry():
            entry = frappe.new_doc("Stock Entry")
            entry.stock_entry_type = "Material Transfer"
            entry.purpose = "Material Transfer"
            entry.set_posting_time = 1
            entry.fg_raw_material_selector = fg_selector_name
            entry.company = company
            return entry

        # Reuse only one existing draft; additional rows go to chunked fresh entries.
        reusable_entry = frappe.get_doc("Stock Entry", existing_open_entry) if existing_open_entry else None
        if reusable_entry:
            existing_qty = sum(flt(r.qty or 0) for r in (reusable_entry.get("items") or []))
            if existing_qty >= RESERVATION_SERIALS_PER_ENTRY:
                # Large stale drafts make submit-time bundle validation very slow.
                # Start fresh chunked entries instead of appending to a huge draft.
                reusable_entry = None

        # Prevent double-booking serials already present in open reservation transfers.
        # Read from both legacy `serial_no` and Serial/Batch Bundle entries.
        used_serial_rows = frappe.db.sql(
            """
            SELECT sbe.serial_no
            FROM `tabStock Entry Detail` sed
            INNER JOIN `tabStock Entry` se ON se.name = sed.parent
            INNER JOIN `tabSerial and Batch Entry` sbe ON sbe.parent = sed.serial_and_batch_bundle
            WHERE se.docstatus < 2
              AND se.purpose = 'Material Transfer'
              AND IFNULL(sbe.serial_no, '') != ''
              AND sed.t_warehouse = %s
              AND sed.s_warehouse IN (%s, %s)
            UNION
            SELECT sed.serial_no
            FROM `tabStock Entry Detail` sed
            INNER JOIN `tabStock Entry` se ON se.name = sed.parent
            WHERE se.docstatus < 2
              AND se.purpose = 'Material Transfer'
              AND IFNULL(sed.serial_no, '') != ''
              AND sed.t_warehouse = %s
              AND sed.s_warehouse IN (%s, %s)
            """,
            (
                reserved_warehouse,
                oc_warehouse,
                rm_warehouse,
                reserved_warehouse,
                oc_warehouse,
                rm_warehouse,
            ),
            as_dict=True,
        )
        used_serials = set()
        for row in used_serial_rows:
            for token in str(row.serial_no or "").replace("\n", ",").split(","):
                token = token.strip()
                if token:
                    used_serials.add(token)

        try_consume_bin = _init_reservation_bin_tracker(reusable_entry)
        skipped_insufficient_bin = []

        # Bulk-load ``stock_uom`` for every involved Item ONCE (was N queries inside the loop).
        unique_item_codes = list({row.item_code for row in serial_dict.values() if row.item_code})
        stock_uom_map = {}
        if unique_item_codes:
            for item_row in frappe.get_all(
                "Item",
                filters={"name": ["in", unique_item_codes]},
                fields=["name", "stock_uom"],
            ):
                stock_uom_map[item_row.name] = item_row.stock_uom or "Nos"

        # Phase 1: validate serials and bucket them by (item_code, s_warehouse, custom_length).
        # Grouping turns one Serial-and-Batch Bundle per serial into one bundle per group,
        # which is the dominant cost on submit (`check_future_entries_exists`, bin updates,
        # bundle validation). RM bars share lengths so they collapse heavily; OC pieces with
        # unique remaining lengths stay 1-per-row, which is acceptable.
        groups = {}  # (item, s_wh, length) -> {meta + "serials": [list]}
        for sn in serial_nos:
            info = piece_map.get(sn)
            if not info:
                continue

            s_detail = serial_dict.get(sn)
            if not s_detail:
                continue

            if s_detail.warehouse != info["s_warehouse"]:
                continue

            if sn in used_serials:
                # Already referenced in another open transfer; skip to avoid phantom reserve.
                continue

            if not try_consume_bin(s_detail.item_code, info["s_warehouse"], 1):
                skipped_insufficient_bin.append(
                    {
                        "serial_no": sn,
                        "item_code": s_detail.item_code,
                        "warehouse": info["s_warehouse"],
                    }
                )
                continue

            item_uom = stock_uom_map.get(s_detail.item_code) or "Nos"
            length = flt(s_detail.custom_length)
            key = (s_detail.item_code, info["s_warehouse"], length)
            grp = groups.get(key)
            if grp is None:
                grp = {
                    "item_code": s_detail.item_code,
                    "uom": item_uom,
                    "stock_uom": item_uom,
                    "s_warehouse": info["s_warehouse"],
                    "t_warehouse": reserved_warehouse,
                    "custom_length": length,
                    "cost_center": cost_center,
                    "serials": [],
                }
                groups[key] = grp
            grp["serials"].append(sn)
            used_serials.add(sn)

        # Phase 2: materialise grouped rows. Cap each row's serial list so a single
        # Stock Entry Detail / Bundle does not balloon past RESERVATION_SERIALS_PER_ROW.
        pending_items = []
        for grp in groups.values():
            serials = grp["serials"]
            for offset in range(0, len(serials), RESERVATION_SERIALS_PER_ROW):
                chunk_serials = serials[offset : offset + RESERVATION_SERIALS_PER_ROW]
                qty = len(chunk_serials)
                if qty == 0:
                    continue
                pending_items.append({
                    "item_code": grp["item_code"],
                    "qty": qty,
                    "uom": grp["uom"],
                    "stock_uom": grp["stock_uom"],
                    "serial_no": "\n".join(chunk_serials),
                    "s_warehouse": grp["s_warehouse"],
                    "t_warehouse": grp["t_warehouse"],
                    "custom_length": grp["custom_length"],
                    "custom_total_length": flt(grp["custom_length"]) * qty,
                    "cost_center": grp["cost_center"],
                })

        if not pending_items:
            msg = "No valid items to reserve after validation."
            if skipped_insufficient_bin:
                sample = skipped_insufficient_bin[0]
                msg = (
                    "No stock could be reserved: warehouse balance for the source item/warehouse "
                    "is insufficient (or negative) for at least one allocated serial — "
                    f"e.g. {sample.get('item_code')} at {sample.get('warehouse')}. "
                    "Fix Bin/stock ledger or serial–warehouse alignment, then retry."
                )
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': msg,
                    'indicator': 'orange',
                    'alert': True
                },
                user=user
            )
            return

        created_entries = []

        def _take_chunk(rows, max_serials):
            """Pop and return a prefix of ``rows`` whose total qty <= max_serials.

            Always returns at least one row even if its qty alone exceeds the cap
            (a single grouped row is already capped to RESERVATION_SERIALS_PER_ROW).
            """
            taken = []
            taken_qty = 0
            while rows:
                next_qty = int(rows[0].get("qty") or 0)
                if taken and taken_qty + next_qty > max_serials:
                    break
                taken.append(rows.pop(0))
                taken_qty += next_qty
            return taken

        # First consume pending rows in reusable draft (if present), bounded by the
        # serial-count cap; then chunk the rest into fresh Stock Entries.
        if reusable_entry and reusable_entry.docstatus == 0:
            first_chunk = _take_chunk(pending_items, RESERVATION_SERIALS_PER_ENTRY)
            for item_row in first_chunk:
                reusable_entry.append("items", item_row)

            if reusable_entry.items:
                frappe.db.connect()
                reusable_entry.save()
                # Stock Entry before_submit hook clears legacy serial/batch fields once
                # ERPNext has converted them into a Serial and Batch Bundle.
                reusable_entry.submit()
                created_entries.append(reusable_entry.name)

        while pending_items:
            chunk_rows = _take_chunk(pending_items, RESERVATION_SERIALS_PER_ENTRY)
            if not chunk_rows:
                break

            entry = _new_reservation_entry()
            for item_row in chunk_rows:
                entry.append("items", item_row)

            frappe.db.connect()
            entry.save()
            entry.submit()
            created_entries.append(entry.name)

        if not created_entries:
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': 'No Stock Entry could be created for reservation.',
                    'indicator': 'orange',
                    'alert': True
                },
                user=user
            )
            return

        # Update raw_materials rows
        for row in doc.raw_materials:
            if row.status == "IS":
                row.warehouse = reserved_warehouse
                row.reserve_tag = 1
                # Link to first created transfer to preserve existing single-link semantics.
                row.stock_entry = created_entries[0]

        doc.save(ignore_permissions=True)

        frappe.publish_realtime(
            event='stock_reservation_done',
            message={
                'status': 'success',
                'message': f'Stock reserved successfully via {len(created_entries)} Stock Entr{"y" if len(created_entries) == 1 else "ies"}',
                'stock_entry': created_entries[0],
                'stock_entries': created_entries,
                'docname': fg_selector_name
            },
            user=user
        )

    except Exception as e:
        error_msg = str(e)

        frappe.publish_realtime(
            event='stock_reservation_done',
            message={
                'status': 'error',
                'message': f'Stock reservation failed: {error_msg}',
                'docname': fg_selector_name
            },
            user=user
        )

@frappe.whitelist()
def return_unconsumed_reserved_stock(fg_selector_name):
    """Return unconsumed stock from Reserved warehouse back to default."""
    doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
    source_warehouse = getattr(doc, "reserved_warehouse", None) or "Reserve - Trial - Sbs"
    default_warehouse = getattr(doc, "raw_material_warehouse", None) or "RM - Trial - Sbs"

    company = getattr(doc, "company", None) or frappe.defaults.get_user_default("company")
    cost_center = getattr(doc, "cost_center", None) or frappe.defaults.get_user_default("cost_center")

    if not company:
        frappe.throw("Company is required. Please set company in FG Raw Material Selector or user defaults.")

    candidate_rows = [
        row for row in doc.raw_materials
        if row.reserve_tag and row.status == "IS" and row.warehouse == source_warehouse
    ]
    if not candidate_rows:
        return {"status": "fail", "message": "No reserved items to return."}

    # Bulk-fetch ``has_serial_no`` for every involved item in one query (was N queries).
    unique_items = list({r.item_code for r in candidate_rows if r.item_code})
    has_serial_map = {}
    if unique_items:
        for it in frappe.get_all(
            "Item",
            filters={"name": ["in", unique_items]},
            fields=["name", "has_serial_no"],
        ):
            has_serial_map[it.name] = cint(it.has_serial_no)

    # Bulk-fetch all serials currently in the reserved warehouse for involved items.
    serials_by_item = defaultdict(list)
    if unique_items:
        for sn in frappe.get_all(
            "Serial No",
            filters={
                "item_code": ["in", unique_items],
                "warehouse": source_warehouse,
                "custom_length": [">", 0],
            },
            fields=["serial_no", "item_code", "custom_length"],
            order_by="custom_length asc",
        ):
            serials_by_item[sn.item_code].append(sn)

    entry = frappe.new_doc("Stock Entry")
    entry.stock_entry_type = "Material Transfer"
    entry.purpose = "Material Transfer"
    entry.set_posting_time = 1
    entry.fg_raw_material_selector = fg_selector_name
    entry.company = company

    for row in candidate_rows:
        has_serial_no = has_serial_map.get(row.item_code, 0)
        is_length_based = bool(row.length or row.dimension)

        if is_length_based and has_serial_no:
            available_serials = serials_by_item.get(row.item_code, [])
            if available_serials:
                for serial in available_serials:
                    entry.append("items", {
                        "item_code": row.item_code,
                        "qty": 1,
                        "uom": row.uom,
                        "stock_uom": row.uom,
                        "serial_no": serial.serial_no,
                        "s_warehouse": source_warehouse,
                        "t_warehouse": default_warehouse,
                        "custom_length": flt(serial.custom_length),
                        "custom_total_length": flt(serial.custom_length),
                        "cost_center": cost_center,
                    })
            else:
                # No serials left in reserved warehouse — fall back to qty transfer.
                entry.append("items", {
                    "item_code": row.item_code,
                    "qty": flt(row.quantity),
                    "uom": row.uom,
                    "stock_uom": row.uom,
                    "s_warehouse": source_warehouse,
                    "t_warehouse": default_warehouse,
                    "cost_center": cost_center,
                })
        else:
            entry.append("items", {
                "item_code": row.item_code,
                "qty": flt(row.quantity),
                "uom": row.uom,
                "stock_uom": row.uom,
                "s_warehouse": source_warehouse,
                "t_warehouse": default_warehouse,
                "cost_center": cost_center,
            })

        row.warehouse = default_warehouse
        row.reserve_tag = 0
        row.status = "NIS"

    if not entry.items:
        return {"status": "fail", "message": "No reserved items to return."}

    entry.save()
    entry.submit()
    doc.save()

    return {"status": "success", "message": f"Unconsumed stock returned to {default_warehouse}."}


@frappe.whitelist()
def get_available_qty(item_code, uom):
    """Return quantity of item excluding reserved warehouses."""
    warehouses_to_exclude = ["Reserve - Trial - Sbs"]
    warehouses = frappe.get_all("Warehouse", filters={"is_group": 0}, pluck="name")

    total = 0
    for wh in warehouses:
        if wh in warehouses_to_exclude:
            continue
        actual_qty = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": wh}, "actual_qty")
        total += flt(actual_qty or 0)

    return total


@frappe.whitelist()
def get_stock_for_items(items, fg_selector_name=None):
    """
    Fast bulk variant: fetches Bin and Item rows ONCE for the whole list instead of
    issuing one query per (item, warehouse) pair from the previous implementation.
    """
    import json

    items = json.loads(items) if isinstance(items, str) else items

    if fg_selector_name:
        try:
            doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
            offcut_wh = getattr(doc, "offcut_warehouse", None) or "OC - Trial - Sbs"
            raw_material_wh = getattr(doc, "raw_material_warehouse", None) or "RM - Trial - Sbs"
            warehouses_to_check = [offcut_wh, raw_material_wh]
        except Exception:
            warehouses_to_check = ["OC - Trial - Sbs", "RM - Trial - Sbs"]
    else:
        warehouses_to_check = ["OC - Trial - Sbs", "RM - Trial - Sbs"]

    item_codes = list({i.get("item_code") for i in items if i.get("item_code")})
    if not item_codes:
        return items

    # Bulk-fetch stock_uom for every involved Item.
    stock_uom_map = {
        row.name: row.stock_uom
        for row in frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["name", "stock_uom"],
        )
    }

    # Bulk-fetch Bin qtys for all (item, warehouse) pairs in scope.
    bin_qty = {}
    bin_rows = frappe.get_all(
        "Bin",
        filters={
            "item_code": ["in", item_codes],
            "warehouse": ["in", warehouses_to_check],
        },
        fields=["item_code", "warehouse", "actual_qty"],
    )
    for b in bin_rows:
        bin_qty[(b.item_code, b.warehouse)] = flt(b.actual_qty)

    for item in items:
        item_code = item.get("item_code")
        uom = item.get("uom")
        item["available_quantity"] = 0
        item["warehouse"] = ""

        if not item_code:
            continue

        stock_uom = stock_uom_map.get(item_code)
        conv_factor = 1
        if stock_uom and uom and stock_uom != uom:
            from erpnext.stock.doctype.item.item import (
                get_uom_conv_factor as get_uom_conversion_factor,
            )
            try:
                conv_factor = get_uom_conversion_factor(item_code, uom) or 1
            except Exception:
                conv_factor = 1

        for wh in warehouses_to_check:
            actual = bin_qty.get((item_code, wh), 0)
            if actual > 0:
                item["available_quantity"] = flt(actual) / flt(conv_factor or 1)
                item["warehouse"] = wh
                break

    return items


def get_actual_qty(item_code, warehouse, uom):
    bin = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, ["actual_qty"], as_dict=True)
    if not bin:
        return 0
    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
    conversion_factor = 1
    if stock_uom != uom:
        from erpnext.stock.doctype.item.item import get_uom_conv_factor as get_uom_conversion_factor
        try:
            conversion_factor = get_uom_conversion_factor(item_code, uom)
        except:
            conversion_factor = 0
    return flt(bin.actual_qty) / flt(conversion_factor or 1)


def update_serial_no_length_from_bundle(doc, method=None):
    """
    Copies ``custom_length`` from the linked **Stock Entry Detail** to every Serial No
    in the bundle using **one bulk UPDATE** per bundle.

    Why bulk: this hook fires inside the Stock Entry submit transaction; with chunked
    reservation creating many bundles, per-row ``frappe.db.set_value`` dominates submit
    latency — sometimes pushing the worker past its RQ timeout.
    """
    if doc.docstatus != 1:
        return

    if doc.voucher_type != "Stock Entry" or not doc.voucher_detail_no:
        return

    serial_nos = [r.serial_no for r in (getattr(doc, "entries", []) or []) if r.serial_no]
    if not serial_nos:
        return

    custom_length = frappe.db.get_value(
        "Stock Entry Detail",
        doc.voucher_detail_no,
        "custom_length",
    )
    if not flt(custom_length):
        return

    placeholders = ", ".join(["%s"] * len(serial_nos))
    frappe.db.sql(
        f"""
        UPDATE `tabSerial No`
        SET custom_length = %s
        WHERE name IN ({placeholders})
        """,
        (flt(custom_length), *serial_nos),
    )


