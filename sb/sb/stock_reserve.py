import frappe
from frappe.utils import flt
import math


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
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)

        if not doc.rm_oc_simulation:
            frappe.publish_realtime(
                event='msgprint',
                message={
                    'message': 'Please run RM/OC Calculation first to determine optimal reservations.',
                    'indicator': 'red',
                    'alert': True
                },
                user=user
            )
            return

        reserved_warehouse = doc.reserved_warehouse or "Reserved Stock - DD"
        oc_warehouse = doc.offcut_warehouse or "Off-Cut - DD"
        rm_warehouse = doc.raw_material_warehouse or "Raw Material - DD"
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

        # Collect all unique serial numbers
        serial_nos = set()
        piece_map = {}

        for sim in doc.rm_oc_simulation:
            if sim.open_stock_oc_reservation:
                sn = sim.open_stock_oc_reservation
                serial_nos.add(sn)
                piece_map[sn] = {"source_type": "OC", "s_warehouse": oc_warehouse, "item_code": sim.rm_item_code}
            elif sim.open_stock_rm_reservation:
                sn = sim.open_stock_rm_reservation
                serial_nos.add(sn)
                piece_map[sn] = {"source_type": "RM", "s_warehouse": rm_warehouse, "item_code": sim.rm_item_code}

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

        # ONE QUERY — works on ALL Frappe v15 versions
        serial_details = frappe.db.sql("""
            SELECT name, item_code, warehouse, custom_length
            FROM `tabSerial No`
            WHERE name IN (%s)
        """ % ', '.join(['%s'] * len(serial_nos)), tuple(serial_nos), as_dict=True)

        # Convert to dict for fast lookup
        serial_dict = {row.name: row for row in serial_details}

        # Create Stock Entry
        entry = frappe.new_doc("Stock Entry")
        entry.stock_entry_type = "Material Transfer"
        entry.purpose = "Material Transfer"
        entry.set_posting_time = 1
        entry.fg_raw_material_selector = fg_selector_name
        entry.company = company

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

            item_uom = frappe.db.get_value("Item", s_detail.item_code, "stock_uom") or "Nos"

            entry.append("items", {
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

        if not entry.items:
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

        entry.save()

        # Fix for ERPNext v15+: If Serial and Batch Bundle is created, clear legacy serial_no field to avoid validation error on submit
        # Use direct SQL to ensure it is cleared in DB, bypassing any ORM hooks or cache
        frappe.db.sql("""
            UPDATE `tabStock Entry Detail` 
            SET serial_no='', batch_no='' 
            WHERE parent = %s AND serial_and_batch_bundle IS NOT NULL AND serial_and_batch_bundle != ''
        """, (entry.name,))
        
        # Clear document cache to ensure next get_doc fetches fresh data
        frappe.clear_document_cache("Stock Entry", entry.name)
        
        # Re-fetch fresh document
        entry = frappe.get_doc("Stock Entry", entry.name)
        entry.submit()

        # Update raw_materials rows
        for row in doc.raw_materials:
            if row.status == "IS":
                row.warehouse = reserved_warehouse
                row.reserve_tag = 1
                row.stock_entry = entry.name

        doc.save(ignore_permissions=True)

        frappe.publish_realtime(
            event='stock_reservation_done',
            message={
                'status': 'success',
                'message': f'Stock reserved successfully via Stock Entry {entry.name}',
                'stock_entry': entry.name,
                'docname': fg_selector_name
            },
            user=user
        )

    except Exception as e:
        frappe.log_error("Stock Reservation Error", f"Error in background stock reservation: {str(e)}")
        frappe.publish_realtime(
            event='msgprint',
            message={
                'message': f'Error reserving stock: {str(e)}',
                'title': 'Stock Reservation Error',
                'indicator': 'red',
                'alert': True
            },
            user=user
        )

@frappe.whitelist()
def return_unconsumed_reserved_stock(fg_selector_name):
    """Return unconsumed stock from Reserved warehouse back to default."""
    doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
    # Use document's warehouses if set, otherwise use defaults
    source_warehouse = getattr(doc, 'reserved_warehouse', None) or "Reserved Stock - DD"
    default_warehouse = getattr(doc, 'raw_material_warehouse', None) or "Raw Material - DD"
    
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
    warehouses_to_exclude = ["Reserved Stock - DD"]
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
            offcut_wh = getattr(doc, 'offcut_warehouse', None) or "Off-Cut - DD"
            raw_material_wh = getattr(doc, 'raw_material_warehouse', None) or "Raw Material - DD"
            warehouses_to_check = [offcut_wh, raw_material_wh]
        except:
            warehouses_to_check = ["Off-Cut - DD", "Raw Material - DD"]
    else:
        warehouses_to_check = ["Off-Cut - DD", "Raw Material - DD"]

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

    frappe.db.commit()
    frappe.log_error(f"Updated {updated} serial numbers for bundle {doc.name}", "Serial Length Sync")


