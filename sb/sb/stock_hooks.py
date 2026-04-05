import frappe
from frappe.utils import cint, flt


def clear_serial_batch_legacy_fields_before_submit(doc, method):
    """
    ERPNext v15+: rows can have both serial_and_batch_bundle and legacy serial_no / batch_no.
    On submit, make_bundle_using_old_serial_batch_fields validates that they match the bundle;
    any mismatch raises "Serial and Batch Bundle ... has already created. Please remove..."
    Once the bundle exists it is authoritative — clear the legacy fields (same as desk message).
    """
    if doc.doctype != "Stock Entry":
        return
    for row in doc.get("items") or []:
        if not row.serial_and_batch_bundle:
            continue
        if row.serial_no or row.batch_no:
            row.serial_no = ""
            row.batch_no = ""


def sync_sle_length_after_submit(doc, method):
    """
    Stock Entry / Purchase Receipt on_submit — runs after ERPNext creates all SLEs.
    Does ONE bulk SQL UPDATE to copy custom_length from voucher lines to all SLEs.
    
    Much faster than per-row hooks: 1 query instead of N queries for N-line entries.
    """
    if doc.doctype not in ("Stock Entry", "Purchase Receipt") or doc.docstatus != 1:
        return

    if doc.doctype == "Stock Entry":
        frappe.db.sql(
            """
            UPDATE `tabStock Ledger Entry` sle
            INNER JOIN `tabStock Entry Detail` sed
                ON sed.name = sle.voucher_detail_no AND sed.parent = sle.voucher_no
            SET
                sle.custom_length = IFNULL(sed.custom_length, 0),
                sle.custom_total_length = IF(
                    IFNULL(sed.custom_length, 0) != 0,
                    IFNULL(sed.custom_length, 0) * sle.actual_qty,
                    IFNULL(sed.custom_total_length, 0)
                )
            WHERE
                sle.voucher_no = %s
                AND sle.voucher_type = 'Stock Entry'
                AND IFNULL(sle.is_cancelled, 0) = 0
                AND (IFNULL(sed.custom_length, 0) != 0 OR IFNULL(sed.custom_total_length, 0) != 0)
            """,
            (doc.name,),
        )
    elif doc.doctype == "Purchase Receipt":
        frappe.db.sql(
            """
            UPDATE `tabStock Ledger Entry` sle
            INNER JOIN `tabPurchase Receipt Item` pri
                ON pri.name = sle.voucher_detail_no AND pri.parent = sle.voucher_no
            SET
                sle.custom_length = IFNULL(pri.custom_length, 0),
                sle.custom_total_length = IF(
                    IFNULL(pri.custom_length, 0) != 0,
                    IFNULL(pri.custom_length, 0) * sle.actual_qty,
                    IFNULL(pri.custom_total_length, 0)
                )
            WHERE
                sle.voucher_no = %s
                AND sle.voucher_type = 'Purchase Receipt'
                AND IFNULL(sle.is_cancelled, 0) = 0
                AND (IFNULL(pri.custom_length, 0) != 0 OR IFNULL(pri.custom_total_length, 0) != 0)
            """,
            (doc.name,),
        )


def apply_custom_length_to_sle(doc):
    """
    Bulk-fix: set SLE lengths from voucher lines using SQL JOIN so each row gets
    custom_total_length = custom_length × actual_qty (per SLE). Used after repost.
    """
    if doc.doctype not in ("Stock Entry", "Purchase Receipt") or doc.docstatus != 1:
        return

    if doc.doctype == "Stock Entry":
        frappe.db.sql(
            """
            UPDATE `tabStock Ledger Entry` sle
            INNER JOIN `tabStock Entry Detail` sed
                ON sed.name = sle.voucher_detail_no AND sed.parent = sle.voucher_no
            SET
                sle.custom_length = IFNULL(sed.custom_length, 0),
                sle.custom_total_length = IF(
                    IFNULL(sed.custom_length, 0) != 0,
                    IFNULL(sed.custom_length, 0) * sle.actual_qty,
                    IFNULL(sed.custom_total_length, 0)
                )
            WHERE
                sle.voucher_no = %s
                AND sle.voucher_type = 'Stock Entry'
                AND IFNULL(sle.is_cancelled, 0) = 0
                AND (IFNULL(sed.custom_length, 0) != 0 OR IFNULL(sed.custom_total_length, 0) != 0)
            """,
            (doc.name,),
        )
    elif doc.doctype == "Purchase Receipt":
        frappe.db.sql(
            """
            UPDATE `tabStock Ledger Entry` sle
            INNER JOIN `tabPurchase Receipt Item` pri
                ON pri.name = sle.voucher_detail_no AND pri.parent = sle.voucher_no
            SET
                sle.custom_length = IFNULL(pri.custom_length, 0),
                sle.custom_total_length = IF(
                    IFNULL(pri.custom_length, 0) != 0,
                    IFNULL(pri.custom_length, 0) * sle.actual_qty,
                    IFNULL(pri.custom_total_length, 0)
                )
            WHERE
                sle.voucher_no = %s
                AND sle.voucher_type = 'Purchase Receipt'
                AND IFNULL(sle.is_cancelled, 0) = 0
                AND (IFNULL(pri.custom_length, 0) != 0 OR IFNULL(pri.custom_total_length, 0) != 0)
            """,
            (doc.name,),
        )


def update_length_in_sle(doc, method=None):
    """Backward-compatible name: bulk refresh from voucher (e.g. after repost)."""
    apply_custom_length_to_sle(doc)

def clear_length_in_sle(doc, method):
    """
    Reset custom_length and custom_total_length in Stock Ledger Entries
    when document is cancelled.
    """
    sle_list = frappe.get_all(
        "Stock Ledger Entry",
        filters={"voucher_no": doc.name},
        fields=["name"]
    )
    for sle in sle_list:
        frappe.db.set_value(
            "Stock Ledger Entry",
            sle.name,
            {
                "custom_length": 0,
                "custom_total_length": 0
            }
        )

def update_serial_no_length(doc, method):
    """
    Update custom_length field in Serial No records from Stock Entry Detail items
    when Stock Entry is submitted.
    
    This updates when use_serial_batch_fields is checked.
    Handles both legacy serial_no field and new serial_and_batch_bundle system.
    When use_serial_batch_fields is checked, custom_length should always be updated.
    """
    from frappe.utils import flt
    
    for item in doc.items:
        # Only process if use_serial_batch_fields is checked
        if not item.use_serial_batch_fields:
            continue
        
        # Get the length from the item (can be 0, which is valid)
        length = flt(item.custom_length) if item.custom_length else 0
        
        # Get serial numbers from either legacy serial_no field or serial_and_batch_bundle
        serial_nos = []
        
        # Method 1: Legacy serial_no field
        if item.serial_no:
            serial_nos = [s.strip() for s in item.serial_no.split('\n') if s.strip()]
        
        # Method 2: New serial_and_batch_bundle system
        elif item.serial_and_batch_bundle:
            try:
                # Query Serial and Batch Entry directly to get serial numbers
                bundle_entries = frappe.get_all(
                    "Serial and Batch Entry",
                    filters={
                        "parent": item.serial_and_batch_bundle,
                        "serial_no": ("is", "set")
                    },
                    fields=["serial_no"],
                    order_by="idx"
                )
                
                # Extract serial_no values, filtering out None/empty values
                serial_nos = [entry.serial_no.strip() for entry in bundle_entries if entry.serial_no and entry.serial_no.strip()]
                
            except Exception as e:
                frappe.log_error(
                    message=f"Error getting serial nos from bundle {item.serial_and_batch_bundle} in Stock Entry {doc.name}: {str(e)}",
                    title="Serial Bundle Error"
                )
                continue
        
        if not serial_nos:
            continue
        
        # Update each Serial No's custom_length
        # When use_serial_batch_fields is checked, always update the length
        updated_count = 0
        for serial_no in serial_nos:
            try:
                # Check if Serial No exists
                if not frappe.db.exists("Serial No", serial_no):
                    continue
                
                # Always update custom_length when use_serial_batch_fields is checked
                frappe.db.set_value("Serial No", serial_no, "custom_length", length)
                updated_count += 1
                
            except Exception as e:
                frappe.log_error(
                    message=f"Error updating Serial No {serial_no} length from Stock Entry {doc.name}: {str(e)}",
                    title="Serial No Length Update Error"
                )
                continue
        
        # Commit after processing each item to ensure updates are saved
        if updated_count > 0:
            frappe.db.commit()




@frappe.whitelist()
def get_ns_mr_shortfall_preview(fg_selector_name):
    """
    Preview NS (not-in-stock) shortfalls for the MR dialog without requiring rm_oc_simulation.
    Uses the same allocator as Reserve Stock / create_material_request_for_shortfall.
    Does not save the FG Raw Material Selector (preview only).

    Returns readable breakdown: per cut length, total mm, suggested bar qty (4820 mm bars),
    same basis as Material Request creation.
    """
    import math

    if not fg_selector_name:
        frappe.throw("FG Raw Material Selector is required")

    doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
    from sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector import (
        _allocate_rm_oc_pieces_for_reservation,
    )

    _serial_nos, _piece_map, ns_shortfalls = _allocate_rm_oc_pieces_for_reservation(doc)

    if not ns_shortfalls:
        return {"empty": True, "items": [], "total_ns_pieces": 0, "distinct_rm": 0}

    std_bar_mm = 4820.0
    items = []
    total_ns_pieces = 0
    grand_total_length_mm = 0.0

    for item_code in sorted(ns_shortfalls.keys()):
        ns = ns_shortfalls[item_code]
        details = [(flt(l), int(q)) for l, q in (ns.get("details") or []) if flt(l) > 0 and int(q) > 0]
        details.sort(key=lambda x: x[0], reverse=True)
        fg_links = ns.get("fg_links") or []

        piece_count = sum(q for _, q in details)
        total_ns_pieces += piece_count

        total_length_mm = sum(l * q for l, q in details)
        grand_total_length_mm += total_length_mm

        if total_length_mm > 0:
            suggested_bar_qty = int(math.ceil(total_length_mm / std_bar_mm))
        else:
            suggested_bar_qty = max(1, piece_count)

        length_rows = []
        for length_mm, qty in details:
            length_rows.append(
                {
                    "length_mm": length_mm,
                    "qty": qty,
                    "line_mm": flt(length_mm * qty),
                }
            )

        item_name = frappe.db.get_value("Item", item_code, "item_name") or ""

        items.append(
            {
                "item_code": item_code,
                "item_name": item_name,
                "pieces": piece_count,
                "total_length_mm": flt(total_length_mm),
                "suggested_bar_qty": suggested_bar_qty,
                "std_bar_mm": std_bar_mm,
                "length_rows": length_rows,
                "fg_codes": sorted(fg_links),
                "fg_codes_display": ", ".join(sorted(fg_links)) if fg_links else "",
            }
        )

    return {
        "empty": False,
        "fg_raw_material_selector": doc.name,
        "items": items,
        "total_ns_pieces": total_ns_pieces,
        "distinct_rm": len(items),
        "grand_total_length_mm": flt(grand_total_length_mm),
        "std_bar_mm": std_bar_mm,
    }


@frappe.whitelist()
def create_material_request_for_shortfall(fg_selector_name):
    from collections import defaultdict   # ← Safe fallback if import above is missing

    try:
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)
        from sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector import (
            _allocate_rm_oc_pieces_for_reservation,
        )

        # Reservation-only allocation (no rm_oc_simulation writes)
        _serial_nos, _piece_map, ns_shortfalls = _allocate_rm_oc_pieces_for_reservation(doc)

        # ns_shortfalls: rm_item_code -> {"details":[(length, qty)], "fg_links":[...]}
        if not ns_shortfalls:
            return "No NS shortfalls to request."

        # Persist IS/NIS on raw_materials (same as reservation flow)
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        # Create Material Request
        mr = frappe.new_doc("Material Request")
        mr.material_request_type = "Purchase"
        mr.transaction_date = frappe.utils.today()
        mr.company = doc.company
        if frappe.db.exists(
            "Custom Field",
            {"dt": "Material Request", "fieldname": "fg_raw_material_selector"},
        ):
            mr.fg_raw_material_selector = doc.name

        # Get warehouse from doc or default
        warehouse = doc.raw_material_warehouse or "RM - Trial - Sbs"

        import math

        for item_code, ns in ns_shortfalls.items():
            details = ns.get("details") or []
            fg_links = ns.get("fg_links") or []

            # details is list of (length, qty) tuples
            total_length = sum(l * q for l, q in details)
            total_pieces = sum(q for l, q in details)
            
            # Calculate quantity: Round Up (Total Length / 4820)
            if total_length > 0:
                qty = math.ceil(total_length / 4820.0)
                qty_description = f"Required Qty: {qty} (based on {total_length}mm / 4820mm)"
            else:
                # Fallback if length is missing/zero: use total pieces
                qty = max(1, math.ceil(total_pieces))
                qty_description = f"Required Qty: {qty} (Length unknown, defaulted to piece count)"

            linked_fgs = ", ".join(sorted(fg_links)) if fg_links else "Multiple"
            # Show individual lengths in description (only unique lengths to avoid spam?)
            # Or show all? Let's show unique lengths with counts if possible, or just list
            # User asked for "length" field to show length.
            # Let's list them.
            length_list_str = ", ".join([f"{int(l)}x{int(q)}" for l, q in details if l > 0])
            
            mr.append("items", {
                "item_code": item_code,
                "qty": qty,
                "uom": frappe.get_value("Item", item_code, "stock_uom") or "Nos",
                "warehouse": warehouse,
                "custom_length": total_length,  # Total length required
                "schedule_date": frappe.utils.add_days(frappe.utils.today(), 7),
                "description": f"Auto-generated from FG Raw Material Selector {doc.name}\n"
                              f"NS Shortfall • {qty_description}\n"
                              f"Lengths: {length_list_str}\n"
                              f"Linked FGs: {linked_fgs}"
            })

        mr.insert(ignore_permissions=True)
        mr.submit()  # Optional: submit directly
        frappe.db.commit()

        return mr.name

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Create MR for Shortfall Failed")
        frappe.throw(f"Failed to create Material Request: {str(e)}")