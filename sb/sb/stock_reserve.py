import frappe
from frappe.utils import flt
import math

RESERVATION_CHUNK_SIZE = 200


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

        # Persist IS/NIS status updates even if reservation exits early.
        doc.save(ignore_permissions=True)

        if not serial_nos:
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

        pending_items = []
        for sn in serial_nos:
            info = piece_map.get(sn)
            if not info:
                continue

            s_detail = serial_dict.get(sn)
            if not s_detail:
                frappe.log_error(f"Serial No {sn} not found", "Stock Reservation Error")
                continue

            if s_detail.warehouse != info["s_warehouse"]:
                frappe.log_error(f"Warehouse mismatch for {sn}: expected {info['s_warehouse']}, found {s_detail.warehouse}")
                continue

            if sn in used_serials:
                # Already referenced in another open transfer; skip to avoid phantom reserve.
                continue

            item_uom = frappe.db.get_value("Item", s_detail.item_code, "stock_uom") or "Nos"

            pending_items.append({
                "item_code": s_detail.item_code,
                "qty": 1,
                "uom": item_uom,
                "stock_uom": item_uom,
                "serial_no": sn,
                "s_warehouse": info["s_warehouse"],
                "t_warehouse": reserved_warehouse,
                "custom_length": flt(s_detail.custom_length),
                "custom_total_length": flt(s_detail.custom_length),
                "cost_center": cost_center
            })
            used_serials.add(sn)

        if not pending_items:
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': 'No valid items to reserve after validation.',
                    'indicator': 'orange',
                    'alert': True
                },
                user=user
            )
            return

        created_entries = []

        # First consume pending rows in reusable draft (if present), then chunk into new entries.
        if reusable_entry and reusable_entry.docstatus == 0:
            for item_row in pending_items[:RESERVATION_CHUNK_SIZE]:
                reusable_entry.append("items", item_row)

            if reusable_entry.items:
                frappe.db.connect()
                reusable_entry.save()
                reusable_entry.submit()
                created_entries.append(reusable_entry.name)

            pending_items = pending_items[RESERVATION_CHUNK_SIZE:]

        for i in range(0, len(pending_items), RESERVATION_CHUNK_SIZE):
            chunk_rows = pending_items[i:i + RESERVATION_CHUNK_SIZE]
            if not chunk_rows:
                continue

            entry = _new_reservation_entry()
            for item_row in chunk_rows:
                entry.append("items", item_row)

            frappe.db.connect()
            entry.save()
            # Let the Stock Entry before_submit hook clear legacy serial/batch fields
            # to keep this flow atomic and avoid mid-transaction DB mutations.
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
        
        # Try to log error with fresh connection
        try:
            frappe.db.connect()
            frappe.log_error(
                message=frappe.get_traceback(),
                title=f"Stock Reservation Error - {fg_selector_name}"
            )
        except:
            pass  # If logging fails, at least send notification
        
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
    # Use document's warehouses if set, otherwise use defaults
    source_warehouse = getattr(doc, 'reserved_warehouse', None) or "Reserve - Trial - Sbs"
    default_warehouse = getattr(doc, 'raw_material_warehouse', None) or "RM - Trial - Sbs"
    
    # Get company and cost_center from document
    company = getattr(doc, 'company', None) or frappe.defaults.get_user_default("company")
    cost_center = getattr(doc, 'cost_center', None) or frappe.defaults.get_user_default("cost_center")
    
    if not company:
        frappe.throw("Company is required. Please set company in FG Raw Material Selector or user defaults.")

    entry = frappe.new_doc("Stock Entry")
    entry.stock_entry_type = "Material Transfer"
    entry.purpose = "Material Transfer"
    entry.set_posting_time = 1
    entry.fg_raw_material_selector = fg_selector_name  # Optional: track origin
    entry.company = company

    for row in doc.raw_materials:
        if row.reserve_tag and row.status == "IS" and row.warehouse == source_warehouse:
            # Check if item is serialized and length-based
            has_serial_no = frappe.db.get_value("Item", row.item_code, "has_serial_no")
            is_length_based = bool(row.length or row.dimension)
            
            if is_length_based and has_serial_no:
                # Parse length from row.length or row.dimension
                required_lengths = []
                if row.length:
                    required_lengths = [flt(v.strip()) for v in str(row.length).split(',') if v.strip() and v.strip() != '-']
                elif row.dimension:
                    required_lengths = [flt(v.strip()) for v in str(row.dimension).split(',') if v.strip() and v.strip() != '-']
                
                if required_lengths:
                    # Get serial numbers that were reserved (from the reserved warehouse)
                    available_serials = frappe.db.get_list(
                        "Serial No",
                        filters={
                            "item_code": row.item_code,
                            "warehouse": source_warehouse,
                            "custom_length": [">", 0]
                        },
                        fields=["serial_no", "custom_length"],
                        order_by="custom_length asc"
                    )
                    
                    if available_serials:
                        # Return each serial back to default warehouse
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
                                "cost_center": cost_center
                            })
                    else:
                        frappe.log_error(
                            message=f"No Serial No found for {row.item_code} in {source_warehouse}",
                            title="Stock Return Error"
                        )
                else:
                    # No valid length, fall back to regular return
                    entry.append("items", {
                        "item_code": row.item_code,
                        "qty": flt(row.quantity),
                        "uom": row.uom,
                        "stock_uom": row.uom,
                        "s_warehouse": source_warehouse,
                        "t_warehouse": default_warehouse,
                        "cost_center": cost_center
                    })
            else:
                # Non-length-based or non-serialized items
                entry.append("items", {
                    "item_code": row.item_code,
                    "qty": flt(row.quantity),
                    "uom": row.uom,
                    "stock_uom": row.uom,
                    "s_warehouse": source_warehouse,
                    "t_warehouse": default_warehouse,
                    "cost_center": cost_center
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
    import json
    items = json.loads(items) if isinstance(items, str) else items

    # Use document's warehouses if fg_selector_name is provided, otherwise use defaults
    if fg_selector_name:
        try:
            doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
            offcut_wh = getattr(doc, 'offcut_warehouse', None) or "OC - Trial - Sbs"
            raw_material_wh = getattr(doc, 'raw_material_warehouse', None) or "RM - Trial - Sbs"
            warehouses_to_check = [offcut_wh, raw_material_wh]
        except:
            warehouses_to_check = ["OC - Trial - Sbs", "RM - Trial - Sbs"]
    else:
        warehouses_to_check = ["OC - Trial - Sbs", "RM - Trial - Sbs"]

    for item in items:
        item_code = item.get("item_code")
        uom = item.get("uom")
        item["available_quantity"] = 0
        item["warehouse"] = ""

        for wh in warehouses_to_check:
            qty = get_actual_qty(item_code, wh, uom)
            if qty > 0:
                item["available_quantity"] = qty
                item["warehouse"] = wh
                break  # found in this warehouse

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

import frappe
from frappe.utils import flt

def update_serial_no_length_from_bundle(doc, method=None):
    """
    Copies custom_length from Stock Entry Detail → Serial No,
    triggered when Serial and Batch Bundle is submitted.
    """

    if doc.docstatus != 1:
        return

    updated = 0

    # Loop through serial numbers in the bundle
    for row in getattr(doc, "entries", []):
        serial_no = row.serial_no
        voucher_detail_no = doc.voucher_detail_no  # <-- CORRECT field linking bundle → stock entry detail

        if not serial_no:
            continue

        # Fetch Stock Entry Detail for this bundle row
        ste_detail = frappe.db.get_value(
            "Stock Entry Detail",
            {"name": voucher_detail_no},
            ["custom_length", "item_code"],
            as_dict=True
        )

        if not ste_detail or not flt(ste_detail.custom_length):
            frappe.log_error(
                f"No custom_length found for Serial {serial_no} using Stock Entry Detail {voucher_detail_no}",
                "Serial Length Mapping"
            )
            continue

        # Apply length to Serial No doctype
        frappe.db.set_value(
            "Serial No",
            serial_no,
            "custom_length",
            flt(ste_detail.custom_length),
            update_modified=False
        )

        updated += 1

    frappe.log_error(f"Updated {updated} serial numbers for bundle {doc.name}", "Serial Length Sync")


