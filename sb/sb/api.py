import json
import frappe

@frappe.whitelist()
def create_offcut_item(data):
    from frappe.model.naming import make_autoname

    if isinstance(data, str):
        data = json.loads(data)

    # Validate required fields
    required_fields = ["generated_item_code", "original_length", "original_rm_code", "design_code", "barcode"]
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        frappe.throw(f"Missing required fields: {', '.join(missing)}")

    # Create new Item
    item = frappe.new_doc("Item")
    item.item_code = data['generated_item_code']
    item.item_name = data['generated_item_code']
    item.item_group = "Raw Material"
    item.is_stock_item = 1
    item.disabled = 0
    item.is_off_cut = 1
    item.original_length = data['original_length']
    item.original_rm_code = data['original_rm_code']
    item.design_code = data['design_code']
    item.barcode = data['barcode']
    item.save(ignore_permissions=True)

    # Determine warehouse
    default_warehouse = frappe.get_value("Item Default",{"parent": item.item_code},"default_warehouse"
                        ) or frappe.db.get_single_value("Stock Settings", "default_warehouse") or \
                        "Stores - VD"
    
    # Create Stock Entry
    stock_entry = frappe.new_doc("Stock Entry")
    stock_entry.stock_entry_type = "Material Receipt"
    stock_entry.append("items", {
        "item_code": item.item_code,
        "qty": 1,
        "allow_zero_valuation_rate":1,
        "t_warehouse": frappe.get_value(
            "Item Default",
            {"parent": item.item_code},
            "default_warehouse"
        ) or "Stores - VD"

    })
   
    stock_entry.save(ignore_permissions=True)
    stock_entry.submit()

    return {
        "message": "Item and stock entry created successfully",
        "item_code": item.item_code,
        "stock_entry": stock_entry.name
    }
