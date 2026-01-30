import frappe
from frappe import _
from collections import defaultdict, Counter
from frappe.utils import flt

def execute(filters=None):
    # Define columns for the report
    columns = [
        {
            "label": "Raw Material",
            "fieldname": "rm",
            "fieldtype": "Data",
            "width": 200
        },
        {
            "label": "Remaining Length",
            "fieldname": "remaining_length",
            "fieldtype": "Float",
            "width": 150
        },
        {
            "label": "Quantity",
            "fieldname": "quantity",
            "fieldtype": "Int",
            "width": 100
        }
    ]

    # Check if filter is provided
    fg_selector_name = filters.get("fg_selector_name") if filters else None
    if not fg_selector_name:
        frappe.throw(_("FG Raw Material Selector is required."))

    try:
        # Fetch the FG Raw Material Selector document
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)

        # Group raw materials by item_code and collect all required cut lengths
        rm_groups = defaultdict(list)
        for row in doc.raw_materials:
            if row.dimension and row.quantity > 0:
                dims = [flt(v.strip()) for v in row.dimension.split(',') if v.strip()]
                for _ in range(int(row.quantity)):
                    rm_groups[row.item_code].extend(dims)

        report_data = []
        standard_length = 4820.0  # Standard stock length

        for item_code, cuts in rm_groups.items():
            if not cuts:
                continue

            # Process cuts sequentially
            current_piece_remaining = standard_length
            piece_count = 0
            last_remaining_per_piece = []

            for cut in sorted(cuts, reverse=True):  # Sort cuts in descending order
                if cut > standard_length:
                    frappe.log_error(
                        message=f"Cut length {cut} for {item_code} exceeds standard length {standard_length}",
                        title="Offcut Report Error"
                    )
                    continue

                # If cut doesn't fit in current piece, start a new piece
                if cut > current_piece_remaining:
                    if current_piece_remaining < standard_length:
                        last_remaining_per_piece.append({
                            "rm": item_code,
                            "remaining_length": current_piece_remaining,
                            "quantity": 1
                        })
                    current_piece_remaining = standard_length
                    piece_count += 1

                # Apply the cut
                current_piece_remaining -= cut

            # Add the last piece's remaining length if it's not fully used
            if current_piece_remaining < standard_length and piece_count > 0:
                last_remaining_per_piece.append({
                    "rm": item_code,
                    "remaining_length": current_piece_remaining,
                    "quantity": 1
                })

            # Aggregate quantities for identical remaining lengths
            length_counts = Counter()
            for entry in last_remaining_per_piece:
                length_counts[entry['remaining_length']] += entry['quantity']

            # Add to report data
            for length, qty in length_counts.items():
                report_data.append({
                    "rm": item_code,
                    "remaining_length": length,
                    "quantity": qty
                })

        if not report_data:
            frappe.msgprint(_("No offcuts calculated based on current raw materials."))
            return columns, []

        # Sort report by rm and then by remaining_length descending
        report_data.sort(key=lambda x: (x['rm'], -x['remaining_length']))

        return columns, report_data

    except Exception as e:
        frappe.log_error(message=f"Error generating offcut report: {str(e)}", title="Offcut Report Error")
        frappe.throw(f"Failed to generate offcut report: {str(e)}")

@frappe.whitelist()
def create_offcut_stock_entries_from_report(fg_selector_name):
    from frappe.model.document import Document

    # Get report data
    columns, report_data = execute({"fg_selector_name": fg_selector_name})

    if not report_data:
        frappe.msgprint(_("No offcut stock entries were created. Check error logs for details."))
        return {"message": _("No offcut stock entries were created.")}

    try:
        # Create a single Stock Entry document
        stock_entry = frappe.new_doc("Stock Entry")
        stock_entry.stock_entry_type = "Material Receipt"  # Change if needed
        stock_entry.custom_fg_selector = fg_selector_name  # Optional if you have this custom link field

        # Get default warehouse (or handle via settings)
        default_warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        if not default_warehouse:
            frappe.throw(_("Please set a Default Warehouse in Stock Settings."))

        # Add all items in one Stock Entry
        for row in report_data:
            for _ in range(int(row['quantity'])):
                stock_entry.append("items", {
                    "item_code": row['rm'],
                    "qty": 1,
                    "uom": frappe.db.get_value("Item", row['rm'], "stock_uom") or "Nos",
                    "conversion_factor": 1,
                    "s_warehouse": None,
                    "t_warehouse": default_warehouse,
                    "custom_length": row['remaining_length'],  # ✅ Custom field for length
                    "description": f"Offcut - Remaining Length: {row['remaining_length']} mm"
                })

        # Save and submit Stock Entry
        stock_entry.insert(ignore_permissions=True)
        stock_entry.submit()

        frappe.msgprint(f"✅ Offcut stock entry created: {stock_entry.name}")
        return {"message": f"Offcut stock entry created: {stock_entry.name}"}

    except Exception as e:
        frappe.log_error(f"Failed to create combined stock entry: {str(e)}", "Offcut Stock Entry Error")
        frappe.throw(f"Error creating combined stock entry: {str(e)}")
