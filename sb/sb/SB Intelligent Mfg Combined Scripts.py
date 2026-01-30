# === Script 1: FG Nesting & RM Reservation Script ===
# Trigger: On BOM Submission or Production Plan Submission

import frappe
from frappe.utils import now_datetime

def reserve_material_and_generate_nesting(doc, method):
    fg_list = get_fg_details_from_bom(doc)
    oc_inventory = get_current_oc_inventory()
    rm_inventory = get_current_rm_inventory()

    for fg in fg_list:
        fg_code = fg['item_code']
        length = fg['length']
        width = fg['width']
        section = fg['section']

        # Fetch complexity, customer priority
        complexity = get_complexity(fg_code)
        priority_score = get_customer_priority(fg['customer'])

        # Find matching OC first
        oc_match = find_matching_oc(section, width, length, oc_inventory)
        if oc_match:
            reserve_oc(oc_match, fg_code)
        else:
            rm_match = find_matching_rm(section, width, length, rm_inventory)
            if rm_match:
                reserve_rm(rm_match, fg_code)
            else:
                raise_material_request(section, width, length, fg_code)

        create_cut_plan_entry(fg_code, oc_match or rm_match)


def get_fg_details_from_bom(doc):
    return [
        {
            'item_code': row.item_code,
            'length': row.length,
            'width': row.width,
            'section': row.section,
            'customer': row.customer
        } for row in doc.items
    ]

# Add helper functions: get_complexity(), get_customer_priority(),
# get_current_oc_inventory(), get_current_rm_inventory(),
# find_matching_oc(), reserve_oc(), find_matching_rm(), reserve_rm(),
# raise_material_request(), create_cut_plan_entry()

# === Script 2: Production Plan Generator ===
# Trigger: Manual or Auto, Scheduled Daily

def generate_production_plan():
    fg_queue = get_pending_fg_queue()
    machine_data = get_machine_status()
    manpower = get_manpower_availability()

    for fg in fg_queue:
        best_slot = find_optimal_machine_slot(fg, machine_data, manpower)
        if best_slot:
            assign_to_schedule(fg, best_slot)

        update_planner_dashboard(fg, best_slot)

# Add helper functions: get_pending_fg_queue(), get_machine_status(),
# get_manpower_availability(), find_optimal_machine_slot(), assign_to_schedule(), update_planner_dashboard()

# === Script 3: OC Barcode Scanner Handler ===
# Trigger: On OC Scan (Mobile App/Custom Entry)

def process_oc_scan(oc_code, section, length, width):
    frappe.get_doc({
        'doctype': 'Item',
        'item_code': oc_code,
        'item_name': f"OC-{section}-{width}-{length}",
        'item_group': 'Off-Cut Inventory',
        'stock_uom': 'Nos',
        'is_stock_item': 1
    }).insert(ignore_permissions=True)

    frappe.get_doc({
        'doctype': 'Stock Entry',
        'purpose': 'Material Receipt',
        'items': [{
            'item_code': oc_code,
            'qty': 1,
            'uom': 'Nos',
            't_warehouse': 'Off-Cut - SB'
        }]
    }).insert(ignore_permissions=True).submit()

# === Script 4: FG Production Completion via Barcode ===
# Trigger: On FG Barcode Scan

def complete_fg_production(fg_code):
    frappe.get_doc({
        'doctype': 'Stock Entry',
        'purpose': 'Manufacture',
        'items': [{
            'item_code': fg_code,
            'qty': 1,
            'uom': 'Nos',
            't_warehouse': 'Finished Goods - SB'
        }]
    }).insert(ignore_permissions=True).submit()

# === Script 5: ML Adaptive Planner ===
# Trigger: Scheduled Weekly / On Demand

def run_ml_planner():
    job_data = get_job_card_data()
    model = train_or_update_model(job_data)

    predictions = model.predict(job_data)
    update_complexity_scores(predictions)
    update_estimated_times(predictions)

    notify_planner(predictions)

# Add helper functions: get_job_card_data(), train_or_update_model(),
# update_complexity_scores(), update_estimated_times(), notify_planner()

# === Barcode-based Issuance ===
# RM Barcode Scan (via Custom Field in Stock Entry) triggers matching and validation

# === General Note ===
# - All scripts are embedded as server-side methods or API endpoints in custom app
# - Can use Custom Doctypes (e.g. FG Queue, OC Inventory, Complexity Scores)
# - Recommend adding dashboards and exceptions log
# - Barcode scanning is done using USB/QR scanners linked to custom entry screens
