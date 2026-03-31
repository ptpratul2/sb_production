import frappe
from frappe.utils import cint, flt

def update_length_in_sle(doc, method):
    """
    Copy custom_length and custom_total_length from items into Stock Ledger Entries
    after document is submitted.
    """
    for item in doc.items:
        if not item.custom_length and not item.custom_total_length:
            continue

        sle_list = frappe.get_all(
            "Stock Ledger Entry",
            filters={"voucher_no": doc.name, "voucher_detail_no": item.name},
            fields=["name"]
        )
        for sle in sle_list:
            frappe.db.set_value(
                "Stock Ledger Entry",
                sle.name,
                {
                    "custom_length": item.custom_length,
                    "custom_total_length": item.custom_total_length or (
                        item.custom_length * item.qty if item.custom_length and item.qty else 0
                    )
                }
            )

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
def create_material_request_for_shortfall(fg_selector_name):
    from collections import defaultdict   # ← Safe fallback if import above is missing

    try:
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)

        # Collect NS rows (ns_flag = 1) from rm_oc_simulation
        shortfalls = defaultdict(list)
        fg_links = defaultdict(set)

        for row in doc.rm_oc_simulation or []:
            if cint(row.ns_flag) == 1:  # This is NS (Not in Stock)
                item_code = row.rm_item_code
                # Each row in rm_oc_simulation represents ONE physical piece (expanded view)
                row_qty = flt(row.qty_of_rm) 
                
                # Extract length from cutting_code (e.g., "C-125-1250" -> 1250)
                try:
                    if row.cutting_code:
                        length = flt(row.cutting_code.split('-')[-1])
                    else:
                        length = 0
                except:
                    length = 0
                
                shortfalls[item_code].append((length, row_qty))
                
                if row.fg_code:
                    fg_links[item_code].add(row.fg_code)

        if not shortfalls:
            return "No NS shortfalls to request."

        # Create Material Request
        mr = frappe.new_doc("Material Request")
        mr.material_request_type = "Purchase"
        mr.transaction_date = frappe.utils.today()
        mr.company = doc.company
        mr.fg_raw_material_selector = doc.name  # Optional reference

        # Get warehouse from doc or default
        warehouse = doc.raw_material_warehouse or "RM - Trial - Sbs"

        import math

        for item_code, details in shortfalls.items():
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

            linked_fgs = ", ".join(sorted(fg_links[item_code])) if fg_links[item_code] else "Multiple"
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