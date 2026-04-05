# fg_raw_material_selector.py
# Copyright (c) 2025
# For license information, see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
import json
from frappe.utils.background_jobs import enqueue
from frappe.utils import flt,cint
import math
from collections import defaultdict

class FGRawMaterialSelector(Document):
    @staticmethod
    def clean_fg_code(fg_code):
        """
        Remove (GD) from FG code parts
        Example: "350|-|B(GD)|1500|-" -> "350|-|B|1500|-"
        """
        if not fg_code or not isinstance(fg_code, str):
            return fg_code
        
        parts = fg_code.split('|')
        if len(parts) >= 3:
            # Clean the code part (index 2) by removing (GD)
            parts[2] = parts[2].replace('(GD)', '').strip()
            # Reconstruct the FG code
            return '|'.join(parts)
        return fg_code
    
    def before_save(self):
        """Set default status if not set"""
        if not self.status:
            self.status = "Draft"
        
        # Set default company if not set
        if not self.company:
            self.company = frappe.defaults.get_user_default("company")
        
        # Auto-update status based on raw materials
        if self.raw_materials and len(self.raw_materials) > 0 and self.status == "Draft":
            self.status = "In Progress"
    
    def validate(self):
        """Validate FG Raw Material Selector"""
        frappe.log_error(
            message=f"Validating FG Raw Material Selector: {self.name}",
            title="FG Validation Debug"
        )
        
        # Set planning date if not set
        if not self.planning_date:
            self.planning_date = frappe.utils.today()
        
        # Validate issued quantities don't exceed remaining
        if self.docstatus == 0:  # Only validate in draft
            self.validate_issued_quantities()
    
    def validate_issued_quantities(self):
        """Ensure issued quantities don't exceed remaining quantities"""
        for item in self.raw_materials:
            if not item.planning_bom_item_reference:
                continue
            
            try:
                pbom_item = frappe.get_doc("FG Components", item.planning_bom_item_reference)
                remaining = flt(pbom_item.remaining_qty)
                issued = flt(item.quantity)
                
                if issued > remaining:
                    frappe.msgprint(
                        f"Row #{item.idx}: Issued quantity ({issued}) exceeds "
                        f"remaining quantity ({remaining}) for {item.fg_code}",
                        indicator="orange",
                        alert=True
                    )
            except Exception as e:
                frappe.log_error(f"Error validating quantity for row {item.idx}: {str(e)}", "FG Quantity Validation")
    
    def on_submit(self):
        """Update processed quantities in Planning BOM on submission"""
        self.update_planning_bom_quantities()
        self.update_project_design_upload_quantities()
        self.status = "Submitted"
        self.db_update()
    
    def on_cancel(self):
        """Reverse processed quantities on cancellation"""
        self.reverse_planning_bom_quantities()
        self.reverse_project_design_upload_quantities()
        self.status = "Cancelled"
        self.db_update()
    
    def update_planning_bom_quantities(self):
        """Update processed_qty in Planning BOM items"""
        pbom_updates = {}  # Track updates per Planning BOM
        pbom_item_qty_map = {}  # Track total quantity per Planning BOM item to avoid duplicates
        
        # First, aggregate quantities by Planning BOM item
        # This handles cases where one Planning BOM item generates multiple raw materials
        for item in self.raw_materials:
            if not item.planning_bom_item_reference:
                continue
            
            pbom_item_name = item.planning_bom_item_reference
            
            # Only count each Planning BOM item once, using the bom_qty field
            # which represents the original quantity from the Planning BOM
            if pbom_item_name not in pbom_item_qty_map:
                # Use bom_qty if available, otherwise use quantity
                issued_qty = flt(item.bom_qty) if item.bom_qty else flt(item.quantity)
                pbom_item_qty_map[pbom_item_name] = issued_qty
        
        # Now update each Planning BOM item only once with the aggregated quantity
        for pbom_item_name, issued_qty in pbom_item_qty_map.items():
            # Get current values
            pbom_item = frappe.get_doc("FG Components", pbom_item_name)
            new_processed = flt(pbom_item.processed_qty) + issued_qty
            new_remaining = flt(pbom_item.required_qty or pbom_item.quantity) - new_processed
            
            # Update the item
            frappe.db.set_value("FG Components", pbom_item_name, {
                "processed_qty": new_processed,
                "remaining_qty": max(0, new_remaining)
            })
            
            # Track which Planning BOMs need status updates
            pbom_name = pbom_item.parent
            if pbom_name not in pbom_updates:
                pbom_updates[pbom_name] = []
            pbom_updates[pbom_name].append(pbom_item_name)
        
        # Update Planning BOM statuses
        for pbom_name in pbom_updates:
            self.update_planning_bom_status(pbom_name)
    
    def update_planning_bom_status(self, pbom_name):
        """Update Planning BOM status based on processed quantities"""
        pbom_doc = frappe.get_doc("Planning BOM", pbom_name)
        pbom_doc.save(ignore_permissions=True)  # This triggers before_save which updates status
        
        # Propagate to Project Design Uploads
        self.update_pdu_status_from_pbom(pbom_doc)
    
    def update_project_design_upload_quantities(self):
        """Update processed quantities in Project Design Upload items"""
        pdu_item_updates = {}
        pdu_item_qty_map = {}  # Track total quantity per PDU item to avoid duplicates
        
        # First, aggregate quantities by PDU item
        # This handles cases where one Planning BOM item (linked to one PDU item) generates multiple raw materials
        for item in self.raw_materials:
            if not item.planning_bom_item_reference:
                continue
            
            # Get the Planning BOM item to find linked PDU item
            pbom_item = frappe.get_doc("FG Components", item.planning_bom_item_reference)
            pdu_item_name = pbom_item.project_design_upload_item
            
            if not pdu_item_name:
                continue
            
            # Only count each PDU item once, using the bom_qty field
            if pdu_item_name not in pdu_item_qty_map:
                # Use bom_qty if available, otherwise use quantity
                issued_qty = flt(item.bom_qty) if item.bom_qty else flt(item.quantity)
                pdu_item_qty_map[pdu_item_name] = {
                    'quantity': issued_qty,
                    'pbom_item': pbom_item
                }
        
        # Now update each PDU item only once with the aggregated quantity
        for pdu_item_name, data in pdu_item_qty_map.items():
            issued_qty = data['quantity']
            pbom_item = data['pbom_item']
            
            # Update PDU item
            pdu_item = frappe.get_doc("Project Design Upload Item", pdu_item_name)
            new_processed = flt(pdu_item.processed_qty) + issued_qty
            new_remaining = flt(pdu_item.quantity) - new_processed
            
            frappe.db.set_value("Project Design Upload Item", pdu_item_name, {
                "processed_qty": new_processed,
                "remaining_qty": max(0, new_remaining)
            })
            
            # Track PDU for status update
            pdu_name = pdu_item.parent
            if pdu_name not in pdu_item_updates:
                pdu_item_updates[pdu_name] = []
            pdu_item_updates[pdu_name].append(pdu_item_name)
        
        # Update PDU statuses
        for pdu_name in pdu_item_updates:
            self.update_pdu_status(pdu_name)
    
    def update_pdu_status(self, pdu_name):
        """Update Project Design Upload status based on processed quantities"""
        pdu_doc = frappe.get_doc("Project Design Upload", pdu_name)
        
        total_qty = sum(flt(item.quantity) for item in pdu_doc.items)
        total_processed = sum(flt(item.processed_qty) for item in pdu_doc.items)
        
        if total_qty == 0:
            return
        
        if total_processed >= total_qty:
            pdu_doc.processed_status = "Processed"
        elif total_processed > 0:
            pdu_doc.processed_status = "Partially Processed"
        
        pdu_doc.save(ignore_permissions=True)
    
    def update_pdu_status_from_pbom(self, pbom_doc):
        """Update PDU status when Planning BOM is fully processed"""
        # Get all unique PDUs linked to this Planning BOM
        pdu_names = set()
        for item in pbom_doc.items:
            if item.project_design_upload:
                pdu_names.add(item.project_design_upload)
        
        # Update each PDU status
        for pdu_name in pdu_names:
            self.update_pdu_status(pdu_name)
    
    def reverse_planning_bom_quantities(self):
        """Reverse processed quantities when cancelling"""
        pbom_updates = {}
        pbom_item_qty_map = {}  # Track total quantity per Planning BOM item to avoid duplicates
        
        # First, aggregate quantities by Planning BOM item
        for item in self.raw_materials:
            if not item.planning_bom_item_reference:
                continue
            
            pbom_item_name = item.planning_bom_item_reference
            
            # Only count each Planning BOM item once
            if pbom_item_name not in pbom_item_qty_map:
                # Use bom_qty if available, otherwise use quantity
                issued_qty = flt(item.bom_qty) if item.bom_qty else flt(item.quantity)
                pbom_item_qty_map[pbom_item_name] = issued_qty
        
        # Now reverse each Planning BOM item only once
        for pbom_item_name, issued_qty in pbom_item_qty_map.items():
            pbom_item = frappe.get_doc("FG Components", pbom_item_name)
            new_processed = max(0, flt(pbom_item.processed_qty) - issued_qty)
            new_remaining = flt(pbom_item.required_qty or pbom_item.quantity) - new_processed
            
            frappe.db.set_value("FG Components", pbom_item_name, {
                "processed_qty": new_processed,
                "remaining_qty": max(0, new_remaining)
            })
            
            pbom_name = pbom_item.parent
            if pbom_name not in pbom_updates:
                pbom_updates[pbom_name] = []
            pbom_updates[pbom_name].append(pbom_item_name)
        
        for pbom_name in pbom_updates:
            self.update_planning_bom_status(pbom_name)
    
    def reverse_project_design_upload_quantities(self):
        """Reverse PDU quantities when cancelling"""
        pdu_item_updates = {}
        pdu_item_qty_map = {}  # Track total quantity per PDU item to avoid duplicates
        
        # First, aggregate quantities by PDU item
        for item in self.raw_materials:
            if not item.planning_bom_item_reference:
                continue
            
            pbom_item = frappe.get_doc("FG Components", item.planning_bom_item_reference)
            pdu_item_name = pbom_item.project_design_upload_item
            
            if not pdu_item_name:
                continue
            
            # Only count each PDU item once
            if pdu_item_name not in pdu_item_qty_map:
                # Use bom_qty if available, otherwise use quantity
                issued_qty = flt(item.bom_qty) if item.bom_qty else flt(item.quantity)
                pdu_item_qty_map[pdu_item_name] = {
                    'quantity': issued_qty,
                    'pbom_item': pbom_item
                }
        
        # Now reverse each PDU item only once
        for pdu_item_name, data in pdu_item_qty_map.items():
            issued_qty = data['quantity']
            
            pdu_item = frappe.get_doc("Project Design Upload Item", pdu_item_name)
            new_processed = max(0, flt(pdu_item.processed_qty) - issued_qty)
            new_remaining = flt(pdu_item.quantity) - new_processed
            
            frappe.db.set_value("Project Design Upload Item", pdu_item_name, {
                "processed_qty": new_processed,
                "remaining_qty": max(0, new_remaining)
            })
            
            pdu_name = pdu_item.parent
            if pdu_name not in pdu_item_updates:
                pdu_item_updates[pdu_name] = []
            pdu_item_updates[pdu_name].append(pdu_item_name)
        
        for pdu_name in pdu_item_updates:
            self.update_pdu_status(pdu_name)

    def process_fg_codes(self):
        try:
            frappe.log_error(
                message=f"Starting process_fg_codes for document: {self.name}",
                title="FG Process Debug"
            )

            # Collect Planning BOM names from planning_bom child table
            pbom_names = [row.get("planning_bom") for row in self.planning_bom if row.get("planning_bom")]
            frappe.log_error(
                message=f"Processing Planning BOMs: {json.dumps(pbom_names, indent=2)}",
                title="FG Process Debug"
            )

            # if not self.planning_bom or not isinstance(self.planning_bom, list):
            #     frappe.log_error(
            #         message="No Planning BOMs selected or invalid format.",
            #         title="FG Raw Material Error"
            #     )
            #     frappe.throw("No Planning BOMs selected or invalid format.")

            output = []

            valid_fg_codes = (
                ["B", "CP", "CPP", "CPPP", "D", "K", "PC", "PH", "PLB", "SB",
                 "T", "TS", "W", "WR", "WRB", "WRS", "WS", "WX", "WXS"] +
                ["BC", "BCE", "KC", "KCE", "BCY", "KCY", "BCZ", "KCZ"] +
                ["CC", "CCL", "CCR", "IC", "ICB", "ICXB", "ICT", "ICX",
                 "LS", "LSK", "LSL", "LSR", "LSW", "SL", "SLR"] +
                ["SC", "SCE", "SCY", "SCZ", "LSC", "LSCE", "LSCK", "LSCEK", "LSCY", "LSCZ"] +
                ["JL", "JLB", "JLT", "JLX", "JR", "JRB", "JRT", "JRX",
                 "SX", "LSX", "LSXK"] +
                ["SXC", "SXCE", "SXCY", "SXCZ", "LSXC", "LSXCE", "LSXCK", "LSXCEK"] +
                ["PCE", "SBE", "TSE", "WRBSE", "WRSE", "WSE", "WXSE"] +
                ["DP", "EB", "MB", "EC", "ECH", "ECT", "ECX", "ECB",
                 "ECK", "RK"]
            )

            batch_size = 100
            fg_codes_all = []

            for pbom_entry in self.planning_bom:
                pbom_name = pbom_entry.get("planning_bom")
                if not pbom_name or not isinstance(pbom_name, str):
                    frappe.log_error(
                        message=f"Invalid Planning BOM name: {pbom_name}",
                        title="FG Raw Material Error"
                    )
                    continue

                try:
                    pbom_doc = frappe.get_doc("Planning BOM", pbom_name)
                    frappe.log_error(
                        message=f"Fetched Planning BOM: {pbom_name}",
                        title="FG Process Debug"
                    )
                except frappe.DoesNotExistError:
                    frappe.log_error(
                        message=f"Planning BOM {pbom_name} not found",
                        title="FG Raw Material Error"
                    )
                    continue

                pbom_project = pbom_doc.get("project")
                pbom_items = pbom_doc.get("items", [])

                pbom_items_serializable = [
                    {
                        "fg_code": fg.get("fg_code"),
                        "quantity": fg.get("quantity"),
                        "uom": fg.get("uom"),
                        "ipo_name": fg.get("ipo_name"),
                        "dwg_no": fg.get("dwg_no"),
                        "a": fg.get("a"),
                        "b": fg.get("b"),
                        "code": fg.get("code"),
                        "l1": fg.get("l1"),
                        "l2": fg.get("l2"),
                        "u_area": fg.get("u_area")
                    } for fg in pbom_items
                ]
                frappe.log_error(
                    message=f"FG Codes for PBOM {pbom_name}: {json.dumps(pbom_items_serializable, indent=2)}",
                    title="FG Process Debug"
                )

                if not pbom_items:
                    frappe.msgprint(f"No FG Components found for Planning BOM: {pbom_name}")
                    frappe.log_error(
                        message=f"No FG Components found for Planning BOM: {pbom_name}",
                        title="FG Raw Material Error"
                    )
                    continue

                fg_codes_all.extend([(fg_component, pbom_project, pbom_name) for fg_component in pbom_items])

            if fg_codes_all:
                self.raw_materials = []
                frappe.log_error(
                    message="Cleared existing raw_materials table.",
                    title="FG Process Debug"
                )
            else:
                frappe.log_error(
                    message="No FG codes to process. Skipping raw_materials clear.",
                    title="FG Process Debug"
                )

            # Process in batches
            for i in range(0, len(fg_codes_all), batch_size):
                batch = fg_codes_all[i:i + batch_size]
                for fg_component, pbom_project, pbom_name in batch:
                    fg_code = fg_component.get("fg_code")
                    component_quantity = fg_component.get("quantity", 1)
                    component_uom = fg_component.get("uom")
                    ipo_name = fg_component.get("ipo_name")
                    dwg_no = fg_component.get("dwg_no", "")
                    a = fg_component.get("a", 0)
                    b = fg_component.get("b", 0)
                    sec_code = fg_component.get("code", "")
                    l1 = fg_component.get("l1", 0)
                    l2 = fg_component.get("l2", 0)
                    bom_qty = fg_component.get("quantity", 0)
                    planning_bom_item_reference = fg_component.get("name")

                    if not fg_code or not isinstance(fg_code, str):
                        frappe.log_error(
                            message=f"Invalid FG Code in PBOM: {pbom_name}, FG Code: {fg_code}",
                            title="FG Raw Material Error"
                        )
                        continue

                    # Clean FG code by removing (GD) from code part
                    fg_code = self.clean_fg_code(fg_code)
                    # Also clean sec_code if it has (GD)
                    if sec_code:
                        sec_code = sec_code.replace('(GD)', '').strip()

                    parts = fg_code.split('|')
                    if len(parts) != 5 or parts[2] not in valid_fg_codes:
                        frappe.log_error(
                            message=f"Skipping invalid FG Code: {fg_code} in PBOM: {pbom_name}",
                            title="FG Raw Material Error"
                        )
                        continue

                    try:
                        # Process with tolerance (add_tolerance=True) for both RM and child parts
                        raw_materials = self.process_single_fg_code(fg_code, add_tolerance=True)
                        frappe.log_error(
                            message=f"Processed FG Code {fg_code}: {json.dumps(raw_materials, indent=2)}",
                            title="FG Process Debug"
                        )
                        if not isinstance(raw_materials, list):
                            frappe.log_error(
                                message=f"Invalid data for FG Code '{fg_code}': {raw_materials}",
                                title="FG Raw Material Error"
                            )
                            continue
                    except Exception as e:
                        frappe.log_error(
                            message=f"Error processing FG Code '{fg_code}' in PBOM {pbom_name}: {str(e)}",
                            title="FG Raw Material Error"
                        )
                        continue

                    rm_table = []
                    for rm in raw_materials:
                        if not isinstance(rm, dict):
                            frappe.log_error(
                                message=f"Invalid raw material for FG Code '{fg_code}': {rm}",
                                title="FG Raw Material Error"
                            )
                            continue

                        rm_quantity = rm.get("quantity", 1) * component_quantity
                        rm_entry = {
                            "fg_code": fg_code,
                            "raw_material_code": rm.get("code"),
                            "item_code": rm.get("code"),
                            "a": a,
                            "b": b,
                            "code": sec_code,
                            "l1": l1,
                            "l2": l2,
                            "bom_qty": bom_qty,
                            "dimension": rm.get("dimension"),
                            "remark": rm.get("remark"),
                            "quantity": rm_quantity,
                            "project": pbom_project,
                            "ipo_name": ipo_name,
                            "dwg_no": dwg_no,
                            "planning_bom": pbom_name,
                            "status": rm.get("status"),
                            "warehouse": rm.get("warehouse"),
                            "planning_bom_item_reference": planning_bom_item_reference
                        }
                        if component_uom:
                            rm_entry["uom"] = component_uom

                        rm_table.append(rm_entry)

                        try:
                            self.append("raw_materials", rm_entry)
                            frappe.log_error(
                                message=f"Appended raw material for FG Code '{fg_code}': {json.dumps(rm_entry, indent=2)}",
                                title="FG Process Debug"
                            )
                        except Exception as e:
                            frappe.log_error(
                                message=f"Error appending raw material for FG Code '{fg_code}': {str(e)}",
                                title="FG Raw Material Error"
                            )
                            continue

                    output.append({
                        "fg_code": fg_code,
                        "planning_bom": pbom_name,
                        "raw_materials": rm_table
                    })

            if output:
                frappe.log_error(
                    message=f"Processing output: {json.dumps(output, indent=2)}",
                    title="FG Raw Material Output"
                )
            else:
                frappe.msgprint("No valid FG codes processed. Check the Error Log for details.")
                frappe.log_error(
                    message="No valid FG codes processed.",
                    title="FG Raw Material Error"
                )

            self.save()

            frappe.publish_realtime(
                event='fg_materials_done',
                message='FG Raw Material processing completed successfully.',
                user=frappe.session.user,
                docname=self.name
            )

        except Exception as e:
            frappe.log_error(
                message=f"Error in process_fg_codes: {str(e)}",
                title="FG Raw Material Error"
            )
            frappe.publish_realtime(
                event='msgprint',
                message=f'Error processing FG codes: {str(e)}',
                user=frappe.session.user
            )

    def process_single_fg_code(self, fg_code, add_tolerance=True):
        """
        Process a single FG code and return raw materials
        
        Args:
            fg_code: The FG code to process
            add_tolerance: If True, adds 5mm cutting tolerance to both RM and child part dimensions.
        """
        def safe_int(val, default=0):
            try:
                return int(val)
            except (ValueError, TypeError):
                return default
        
        def check_stock_available(item_code):
            """Check if an item has stock available in warehouses"""
            try:
                # Use document's warehouse fields if set, otherwise use defaults
                offcut_wh = getattr(self, 'offcut_warehouse', None) or "OC - Trial - Sbs"
                raw_material_wh = getattr(self, 'raw_material_warehouse', None) or "RM - Trial - Sbs"
                warehouses_to_check = [offcut_wh, raw_material_wh]
                total_qty = 0
                for warehouse in warehouses_to_check:
                    if warehouse:  # Only check if warehouse is set
                        bin_data = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, ["actual_qty"], as_dict=True)
                        if bin_data and bin_data.actual_qty:
                            total_qty += flt(bin_data.actual_qty)
                return total_qty > 0
            except Exception as e:
                frappe.log_error(message=f"Error checking stock for {item_code}: {str(e)}", title="FG Stock Check Error")
                return False
        
        # Tolerance for Raw Materials only (5mm for cutting loss)
        rm_tolerance = 4 if add_tolerance else 0
        # Child parts tolerance (5mm when tolerance is enabled)
        child_tolerance = 4 if add_tolerance else 0
        
        fg_section_map = {
            'B': 'CH SECTION', 'CP': 'CH SECTION', 'CPP': 'CH SECTION', 'CPPP': 'CH SECTION',
            'D': 'CH SECTION', 'K': 'CH SECTION', 'PC': 'CH SECTION', 'PH': 'CH SECTION',
            'PLB': 'CH SECTION', 'SB': 'CH SECTION', 'T': 'CH SECTION', 'TS': 'CH SECTION',
            'W': 'CH SECTION', 'WR': 'CH SECTION', 'WRB': 'CH SECTION', 'WRS': 'CH SECTION',
            'WS': 'CH SECTION', 'WX': 'CH SECTION', 'WXS': 'CH SECTION',
            'BC': 'CH SECTION CORNER', 'BCE': 'CH SECTION CORNER', 'KC': 'CH SECTION CORNER',
            'KCE': 'CH SECTION CORNER', 'BCY': 'CH SECTION CORNER', 'BCZ': 'CH SECTION CORNER',
            'KCY': 'CH SECTION CORNER', 'KCZ': 'CH SECTION CORNER',
            'CC': 'IC SECTION', 'CCL': 'IC SECTION', 'CCR': 'IC SECTION', 'IC': 'IC SECTION',
            'ICB': 'IC SECTION', 'ICT': 'IC SECTION', 'ICX': 'IC SECTION', 'ICXB': 'IC SECTION',
            'LS': 'IC SECTION', 'LSK': 'IC SECTION', 'LSL': 'IC SECTION', 'LSR': 'IC SECTION',
            'LSW': 'IC SECTION', 'SL': 'IC SECTION', 'SLR': 'IC SECTION',
            'SC': 'IC SECTION CORNER', 'SCE': 'IC SECTION CORNER', 'SCY': 'IC SECTION CORNER',
            'SCZ': 'IC SECTION CORNER', 'LSC': 'IC SECTION CORNER', 'LSCE': 'IC SECTION CORNER',
            'LSCK': 'IC SECTION CORNER', 'LSCEK': 'IC SECTION CORNER', 'LSCY': 'IC SECTION CORNER',
            'LSCZ': 'IC SECTION CORNER',
            'JL': 'J SECTION', 'JLB': 'J SECTION', 'JLT': 'J SECTION', 'JLX': 'J SECTION',
            'JR': 'J SECTION', 'JRB': 'J SECTION', 'JRT': 'J SECTION', 'JRX': 'J SECTION',
            'SX': 'J SECTION', 'LSX': 'J SECTION', 'LSXK': 'J SECTION',
            'SXC': 'J SECTION CORNER', 'SXCE': 'J SECTION CORNER', 'SXCY': 'J SECTION CORNER',
            'SXCZ': 'J SECTION CORNER', 'LSXC': 'J SECTION CORNER', 'LSXCK': 'J SECTION CORNER',
            'LSXCE': 'J SECTION CORNER', 'LSXCEK': 'J SECTION CORNER',
            'PCE': 'T SECTION', 'SBE': 'T SECTION', 'TSE': 'T SECTION', 'WRBSE': 'T SECTION',
            'WRSE': 'T SECTION', 'WSE': 'T SECTION', 'WXSE': 'T SECTION',
            'DP': 'MISC SECTION', 'EB': 'MISC SECTION', 'MB': 'MISC SECTION',
            'EC': 'MISC SECTION', 'ECH': 'MISC SECTION', 'ECT': 'MISC SECTION',
            'ECX': 'MISC SECTION', 'ECB': 'MISC SECTION', 'ECK': 'MISC SECTION', 'RK': 'MISC SECTION'
        }
        
        try:
            # Clean FG code by removing (GD) from code part
            fg_code = self.clean_fg_code(fg_code)
            frappe.log_error(message=f"Processing FG Code (after cleaning): {fg_code}", title="FG Single Code Debug")
            parts = fg_code.split('|')
            if len(parts) != 5:
                frappe.log_error(message=f"Invalid FG Code format. Expected 5 parts, got {len(parts)}: {fg_code}", title="FG Raw Material Error")
                return []
            frappe.log_error(message=f"Server-Side Parts: {parts}", title="FG Single Code Debug")
            a = safe_int(parts[0])
            b = safe_int(parts[1]) if parts[1] else 0
            fg_code_part = parts[2]
            l1 = safe_int(parts[3])
            l2 = safe_int(parts[4]) if parts[4] else 0
            frappe.log_error(message=f"Parsed: A={a}, B={b}, FG_CODE={fg_code_part}, L1={l1}, L2={l2}", title="FG Single Code Debug")
        except Exception as e:
            frappe.log_error(message=f"Error parsing FG Code: {str(e)}", title="FG Raw Material Error")
            return []

        # Define FG groups
        ch_straight = ["B", "CP", "CPP", "CPPP", "D", "K", "PC", "PH", "PLB", "SB", "T", "TS", "W", "WR", "WRS", "WRB", "WS", "WX", "WXS"]
        ch_corner = ["BC", "BCE", "BCY", "KC", "KCE", "BCZ", "KCY", "KCZ"]
        ic_straight = ["CC", "CCL", "CCR", "IC", "ICB", "ICT", "ICX", "ICXB", "LSK", "LS", "LSL", "LSR", "LSW", "SL", "SLR"]
        ic_corner = ["SC", "SCE", "SCY", "SCZ", "LSC", "LSCE", "LSCK", "LSCEK", "LSCY", "LSCZ"]
        j_straight = ["JL", "JLB", "JLT", "JLX", "JR", "JRB", "JRT", "JRX", "SX", "LSX", "LSXK"]
        j_corner = ["SXC", "SXCE", "SXCY", "SXCZ", "LSXC", "LSXCE", "LSXCK", "LSXCEK"]
        t_straight = ["PCE", "SBE", "TSE", "WRBSE", "WRSE", "WSE", "WXSE"]
        misc_straight = ["DP", "EB", "MB", "EC", "ECH", "ECT", "ECX", "ECK", "ECB", "RK"]

        # Channel section mappings
        ch_sections = {
            50: "50 CH", 75: "75 CH", 100: "100 CH", 125: "125 CH",
            150: "150 CH", 175: "175 CH", 200: "200 CH", 250: "250 CH",
            300: "300 CH", 350: "350 CH", 400: "400 CH", 450: "450 CH",
            500: "500 CH", 600: "600 CH"
        }

        # IC section mappings for exact matches
        ic_sections = {
            (100, 100): ("100 IC", "-"),
            (125, 100, "SL"): ("125 SL", "-"),
            (100, 125, "SL"): ("125 SL", "-"),
            (125, 100, "CC"): ("125 SL", "-"),
            (100, 125, "CC"): ("125 SL", "-"),
            (125, 100, "CCR"): ("125 SL", "-"),
            (125, 100, "CCL"): ("125 SL", "-"),
            (125, 100): ("125 IC", "-"),
            (150, 100): ("150 IC", "-"),
            (100, 150): ("150 IC", "-")
        }

        # L-section mapping for IC (single values per range)
        ic_l_section_map = {
            (50, 105): "110 L",
            (106, 125): "130 L",
            (126, 150): "155 L",
            (151, 175): "180 L",
            (176, 200): "205 L",
            (201, 225): "230 L",
            (226, 250): "255 L",
            (251, 275): "280 L",
            (276, 300): "305 L"
        }

        # L-section mappings for CH straight
        ch_l_sections_straight = {
            (51, 105): ("110 L", "MAIN FRAME"),
            (106,125): ("130 L", "MAIN FRAME"),
            (126, 149): ("155 L", "MAIN FRAME"),
            (151, 174): ("180 L", "MAIN FRAME"),
            (176, 199): ("205 L", "MAIN FRAME"),
            (201, 225): ("230 L", "MAIN FRAME"),
            (226, 249): ("255 L", "MAIN FRAME"),
            (251, 275): ("280 L", "MAIN FRAME"),
            (276, 300): ("305 L", "MAIN FRAME"),
            (301, 325): ("180 L", "155 L"),
            (326, 350): ("180 L", "180 L"),
            (351, 375): ("205 L", "180 L"),
            (376, 400): ("205 L", "205 L"),
            (401, 425): ("230 L", "205 L"),
            (426, 450): ("230 L", "230 L"),
            (451, 475): ("255 L", "230 L"),
            (476, 500): ("255 L", "255 L"),
            (501, 525): ("280 L", "255 L"),
            (526, 550): ("280 L", "280 L"),
            (551, 575): ("305 L", "280 L"),
            (576, 600): ("305 L", "305 L")
        }

        # L-section mappings for CH corner
        ch_l_sections_corner = {
            (51, 105): ("110 L", "MAIN FRAME"),
            (106,125): ("130 L", "MAIN FRAME"),
            (126, 149): ("155 L", "MAIN FRAME"),
            (151, 174): ("180 L", "MAIN FRAME"),
            (176, 199): ("205 L", "MAIN FRAME"),
            (201, 225): ("230 L", "MAIN FRAME"),
            (226, 249): ("255 L", "MAIN FRAME"),
            (251, 275): ("280 L", "MAIN FRAME"),
            (276, 300): ("305 L", "MAIN FRAME"),
            (301, 325): ("180 L", "155 L"),
            (326, 350): ("180 L", "180 L"),
            (351, 375): ("205 L", "180 L"),
            (376, 400): ("205 L", "205 L"),
            (401, 425): ("230 L", "205 L"),
            (426, 450): ("230 L", "230 L"),
            (451, 475): ("255 L", "230 L"),
            (476, 500): ("255 L", "255 L"),
            (501, 525): ("280 L", "255 L"),
            (526, 550): ("280 L", "280 L"),
            (551, 575): ("305 L", "280 L"),
            (576, 600): ("305 L", "305 L")
        }

        # J section mappings
        j_sections = {
            (25, 50): ("J SEC", "-"),
            (25, 115): ("115 T", "-"),
            (116, 250): ("AL SHEET", "-")
        }

        j_l_sections = {
            (50, 105): "110 L",
            (106, 125): "130 L",
            (126, 150): "155 L",
            (151, 175): "180 L",
            (176, 200): "205 L",
            (201, 225): "230 L",
            (226, 250): "255 L",
            (251, 275): "280 L",
            (276, 300): "305 L"
        }

        # T section mappings
        t_sections = {
            (230,): ("100 T", "-", "-"),
            (231, 360): ("115 T", "115 T", "-"),
            (380,): ("250 CH", "EC", "-"),
            (430,): ("300 CH", "EC", "-"),
            (361, 380): ("255 L", "MAIN FRAME", "EC"),
            (381, 405): ("280 L", "MAIN FRAME", "EC"),
            (406, 430): ("305 L", "MAIN FRAME", "EC"),
            (431, 455): ("180 L", "155 L", "EC"),
            (456, 480): ("180 L", "180 L", "EC")
        }

        # Misc section mappings
        misc_sections = {
            (100, "EB"): ("EB MB 100", "-"),
            (150, "EB"): ("EB MB 150", "-"),
            (100, "MB"): ("EB MB 100", "-"),
            (150, "MB"): ("EB MB 150", "-"),
            (100, "DP"): ("DP 100", "-"),
            (150, "DP"): ("DP 150", "-"),
            (130,): ("EC", "-"),
            (50, "RK"): ("RK-50", "-"),
            (100, "RK"): ("110 L", "-"),
            (125, "RK"): ("RK-125", "-"),
            (150, "RK"): ("155 L", "-")
        }

        raw_materials = []
        child_parts = []
        degree_cutting = getattr(self, 'degree_cutting', False)
        
        # Initial lengths (no modification needed here - tolerance applied per section)

        if fg_code_part in ch_straight or fg_code_part in ch_corner:
            is_corner = fg_code_part in ch_corner
            section_map = ch_l_sections_corner if is_corner else ch_l_sections_straight
            
            # Apply tolerance to ALL main material dimensions (default case)
            cut_dim1, cut_dim2 = f"{l1+rm_tolerance}", f"{l2+rm_tolerance}" if l2 else "-"
            
            # Special cases that override the default
            if fg_code_part in ["WR", "WRS"]:
                cut_dim1, cut_dim2 = f"{l1-50+rm_tolerance}", f"{l2-50+rm_tolerance}" if l2 else "-"
            elif is_corner:
                if fg_code_part in ["BCE", "KCE"]:
                    cut_dim1, cut_dim2 = f"{l1+65+10+rm_tolerance}", f"{l2+65+10+rm_tolerance}" if l2 else "-"
                elif fg_code_part in ["BCY", "KCY"]:
                    cut_dim1, cut_dim2 = f"{l1+65+10+rm_tolerance}", f"{l2+10+rm_tolerance}" if l2 else "-"
                elif fg_code_part in ["BC", "KC", "BCZ", "KCZ"]:
                    cut_dim1, cut_dim2 = f"{l1+10+rm_tolerance}", f"{l2+10+rm_tolerance}" if l2 else "-"
            
            if a <= 300 and a % 25 == 0 and a in ch_sections:
                rm_code = ch_sections[a]
                cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
                raw_materials.append({"code": rm_code, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a}, using RM1={rm_code}, Cut={cut_dim}", title="CH Section Logic")
            elif a in [350, 400, 450, 500, 600] and a in ch_sections:
                # Check if CH section has stock available
                rm_code = ch_sections[a]
                if check_stock_available(rm_code):
                    # Stock available, use CH section
                    cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
                    raw_materials.append({"code": rm_code, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                    frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a}, using RM1={rm_code} (stock available), Cut={cut_dim}", title="CH Section Logic")
                else:
                    # Stock not available, fall back to L section + L section for A > 300
                    frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a}, {rm_code} not in stock, using L section + L section", title="CH Section Logic")
                    for (min_a, max_a), (rm1, rm2) in section_map.items():
                        if min_a <= a <= max_a:
                            cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
                            raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                            if rm2 != "MAIN FRAME":
                                raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                            else:
                                raw_materials.append({"code": "MAIN FRAME", "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                            frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a} in range {min_a}-{max_a}, using RM1={rm1}, RM2={rm2}, Cut={cut_dim}", title="CH Section Logic")
                            break
            elif a > 300:
                # For A > 300 (other values), use L section + L section from section_map
                for (min_a, max_a), (rm1, rm2) in section_map.items():
                    if min_a <= a <= max_a:
                        cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        if rm2 != "MAIN FRAME":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        else:
                            raw_materials.append({"code": "MAIN FRAME", "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a} in range {min_a}-{max_a}, using RM1={rm1}, RM2={rm2}, Cut={cut_dim}", title="CH Section Logic")
                        break
            else:
                # For A <= 300 but not matching exact CH sections, use L section mapping
                for (min_a, max_a), (rm1, rm2) in section_map.items():
                    if min_a <= a <= max_a:
                        cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        if rm2 != "MAIN FRAME":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        else:
                            raw_materials.append({"code": "MAIN FRAME", "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1})
                        frappe.log_error(message=f"CH {'Corner' if is_corner else 'Straight'}: A={a} in range {min_a}-{max_a}, using RM1={rm1}, RM2={rm2}, Cut={cut_dim}", title="CH Section Logic")
                        break
            
            if fg_code_part != "PLB":
                side_rail_qty = 1 if degree_cutting and fg_code_part in ["WR", "WRB", "WRS"] else 2
                child_parts.append({"code": "SIDE RAIL", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": side_rail_qty})
                raw_materials.append({"code": "SIDE RAIL", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": side_rail_qty})
            if fg_code_part == "WR":
                child_parts.append({"code": "RK-50", "dimension": f"{a+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{a-130+child_tolerance}"})
                raw_materials.append({"code": "RK-50", "dimension": f"{a+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{a-130+child_tolerance}"})

            if fg_code_part in ["CP", "CPP", "CPPP","PH"] :
                quantity = 1 if fg_code_part == "CP" else 2 if fg_code_part == "CPP" else 3 if fg_code_part == "CPPP" else 1
                child_parts.append({"code": "ROUND PIPE", "dimension": f"{146+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{146+child_tolerance}"})
                raw_materials.append({"code": "ROUND PIPE", "dimension": f"{146+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{146+child_tolerance}"})
                child_parts.append({"code": "SQUARE PIPE", "dimension": f"{80+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{80+child_tolerance}"})
                raw_materials.append({"code": "SQUARE PIPE", "dimension": f"{80+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{80+child_tolerance}"})

            if fg_code_part in ["T", "TS", "W", "WR", "WRB", "WS", "WX", "WXS", "WRS"]:
                child_parts.append({"code": "U STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": 8, "length": f"{a-16+child_tolerance}"})
                raw_materials.append({"code": "U STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": 8, "length": f"{a-16+child_tolerance}"})
                child_parts.append({"code": "H STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": 3, "length": f"{a-16+child_tolerance}"})
                raw_materials.append({"code": "H STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": 3, "length": f"{a-16+child_tolerance}"})

            if fg_code_part in ["D", "SB", "PC", "B"]:
                quantity = 4 if fg_code_part in ["D","SB"] else 5
                child_parts.append({"code": "I STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{a-16+child_tolerance}"})
                raw_materials.append({"code": "I STIFFNER", "dimension": f"{a-16+child_tolerance}", "remark": "CHILD PART", "quantity": quantity, "length": f"{a-16+child_tolerance}"})

            if fg_code_part == "K" and l1 >= 1800:
                child_parts.append({"code": "STIFF PLATE", "dimension": f"{a-16+child_tolerance}X{61+child_tolerance}X4", "remark": "CHILD PART", "quantity": 5})
                raw_materials.append({"code": "STIFF PLATE", "dimension": f"{a-16+child_tolerance}X{61+child_tolerance}X4", "remark": "CHILD PART", "quantity": 5})
        
        
        # IC section mappings
        elif fg_code_part in ic_straight or fg_code_part in ic_corner:
            is_corner = fg_code_part in ic_corner
            
            # Apply tolerance to ALL main material dimensions (default case)
            cut_dim1, cut_dim2 = f"{l1+rm_tolerance}", f"{l2+rm_tolerance}" if l2 else "-"
            length1, length2 = l1 + rm_tolerance, l2 + rm_tolerance if l2 else 0
            
            # Special cases that override the default
            if fg_code_part in ["ICX","ICT"]:
                cut_dim1 = f"{l1-8+rm_tolerance}"
                length1 = l1 - 8 + rm_tolerance
            elif fg_code_part in ["IC","ICB","ICXB"]:
                cut_dim1 = f"{l1-4+rm_tolerance}"
                length1 = l1 - 4 + rm_tolerance
            elif fg_code_part in ["SCE", "LSCE", "LSCEK"]:
                cut_dim1, cut_dim2 = f"{l1+b+10+rm_tolerance}", f"{l2+b+10+rm_tolerance}" if l2 else "-"
                length1, length2 = l1 + b + 10 + rm_tolerance, l2 + b + 10 + rm_tolerance if l2 else 0
            elif fg_code_part in ["SCY", "LSCY"]:
                cut_dim1, cut_dim2 = f"{l1+100+10+rm_tolerance}", f"{l2+10+rm_tolerance}" if l2 else "-"
                length1, length2 = l1 + 100 + 10 + rm_tolerance, l2 + 10 + rm_tolerance if l2 else 0
            elif fg_code_part in ["SCZ", "LSCZ"]:
                cut_dim1, cut_dim2 = f"{l1+10+rm_tolerance}", f"{l2+10+rm_tolerance}" if l2 else "-"
                length1, length2 = l1 + 10 + rm_tolerance, l2 + 100 + 10 + rm_tolerance if l2 else 0
            elif fg_code_part in ["SC", "SCE", "SCY", "SCZ", "LSC", "LSCE", "LSCK", "LSCEK", "LSCY", "LSCZ"]:
                cut_dim1, cut_dim2 = f"{l1+10+rm_tolerance}", f"{l2+10+rm_tolerance}" if l2 else "-"
                length1, length2 = l1 + 10 + rm_tolerance, l2 + 10 + rm_tolerance if l2 else 0
            
            cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
            matched = False
            for key in ic_sections:
                if isinstance(key, tuple) and len(key) == 3:
                    if (a, b, fg_code_part) == key or (b, a, fg_code_part) == key:
                        rm1, rm2 = ic_sections[key]
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                        if rm2 != "-":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                        matched = True
                        frappe.log_error(message=f"IC {'Corner' if is_corner else 'Straight'}: A={a}, B={b}, FG={fg_code_part}, using RM1={rm1}, RM2={rm2}, Cut={cut_dim}", title="IC Section Logic")
                        break
                elif isinstance(key, tuple) and len(key) == 2:
                    if (a, b) == key or (b, a) == key:
                        rm1, rm2 = ic_sections[key]
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                        if rm2 != "-":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                        matched = True
                        frappe.log_error(message=f"IC {'Corner' if is_corner else 'Straight'}: A={a}, B={b}, using RM1={rm1}, RM2={rm2}, Cut={cut_dim}", title="IC Section Logic")
                        break
            
            if not matched:
                # Separate lookup for RM1 (based on A) and RM2 (based on B)
                def get_ic_l_rm(size):
                    for (min_r, max_r), code in ic_l_section_map.items():
                        if min_r <= size <= max_r:
                            return code
                    return None

                rm1 = get_ic_l_rm(a)
                rm2 = get_ic_l_rm(b)

                if rm1:
                    remark = "IC SECTION" if rm1.endswith("L") else fg_section_map[fg_code_part]
                    raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": remark, "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                    frappe.log_error(message=f"IC {'Corner' if is_corner else 'Straight'}: A={a}, using RM1={rm1}, Cut={cut_dim}", title="IC Section Logic")
                if rm2:
                    remark = "IC SECTION" if rm2.endswith("L") else fg_section_map[fg_code_part]
                    raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": remark, "quantity": 1, "length": cut_dim1 if not is_corner else f"{length1},{length2}"})
                    frappe.log_error(message=f"IC {'Corner' if is_corner else 'Straight'}: B={b}, using RM2={rm2}, Cut={cut_dim}", title="IC Section Logic")

            # Determine if made with IC or L+L for child part dimensions
            is_made_with_ic = any(rm.get("code", "").endswith("IC") or rm.get("code", "").endswith("SL") for rm in raw_materials)
            
            # Side rail: given in all FG except IC,ICB,ICT,ICX,ICXB
            if fg_code_part not in ["IC", "ICB", "ICT", "ICX", "ICXB"]:
                side_rail_qty = 2 if fg_code_part not in ["SCY", "SCZ", "LSCY", "LSCZ"] else 1
                side_rail_dim = f"{a-15 + child_tolerance}" if is_made_with_ic else f"{a-12 + child_tolerance}"
                child_parts.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
                raw_materials.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
            
            # Stiffner plate: given in all FG
            if fg_code_part in ic_straight or fg_code_part in ic_corner:
                stiff_qty = 5
                stiff_dim = f"{a-15 + child_tolerance}X{b-15 + child_tolerance}X4" if is_made_with_ic else f"{a-12 + child_tolerance}X{b-12 + child_tolerance}X4"
                child_parts.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})
                raw_materials.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})
                
            # Outer cap for IC,ICB,ICT,ICX,ICXB
            if fg_code_part in ["IC", "ICB", "ICXB"]:
                outer_cap_dim = f"{a+child_tolerance}X{b+child_tolerance}X4"
                child_parts.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})
                raw_materials.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})
             # Outer cap for IC,ICB,ICT,ICX,ICXB
            if fg_code_part in ["ICT", "ICX"]:
                outer_cap_dim = f"{a+child_tolerance}X{b+child_tolerance}X4"
                child_parts.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 2, "length": outer_cap_dim})
                raw_materials.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 2, "length": outer_cap_dim})
            
            # Outer cap for corners
            elif fg_code_part in ["SCY", "SCZ", "LSCY", "LSCZ"]:
                outer_cap_dim = f"{a+child_tolerance}X{((b+child_tolerance) / math.sin(math.radians(45))):.2f}X4"
                child_parts.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})
                raw_materials.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})

        elif fg_code_part in j_straight or fg_code_part in j_corner:
            is_corner = fg_code_part in j_corner
            
            # Apply tolerance to ALL main material dimensions (default case)
            cut_dim1, cut_dim2 = f"{l1+rm_tolerance}", f"{l2+rm_tolerance}" if l2 else "-"
            length1, length2 = l1 + rm_tolerance, l2 + rm_tolerance if l2 else 0
            
            # Special cases that override the default
            if fg_code_part in ["JLT", "JRT", "JLX", "JRX"]:
                cut_dim1 = f"{l1-8+rm_tolerance}"
                length1 = l1 - 8 + rm_tolerance
            elif fg_code_part in ["LSX"]:
                cut_dim1 = f"{l1+rm_tolerance}"
                length1 = l1 + rm_tolerance
            elif fg_code_part in ["JL", "JLB", "JR", "JRB"]:
                cut_dim1 = f"{l1-4+rm_tolerance}"
                length1 = l1 - 4 + rm_tolerance
            elif fg_code_part in ["SXC", "SXCE", "SXCZ", "LSXC", "LSXCK", "LSXCE", "LSXCEK"]:
                cut_dim1 = f"{l1+10+rm_tolerance}" if fg_code_part in ["SXC", "SXCZ", "LSXCK"] else f"{l1+b+10+rm_tolerance}"
                cut_dim2 = f"{l2+10+rm_tolerance}" if fg_code_part in ["SXC", "SXCZ", "LSXCK"] else f"{l2+b+10+rm_tolerance}" if l2 else "-"
                length1 = l1 + 10 + rm_tolerance if fg_code_part in ["SXC", "SXCZ", "LSXCK"] else l1 + b + 10 + rm_tolerance
                length2 = l2 + 10 + rm_tolerance if fg_code_part in ["SXC", "SXCZ", "LSXCK"] else l2 + b + 10 + rm_tolerance if l2 else 0
            elif fg_code_part in ["SXCY"]:
                cut_dim1 = f"{l1+10+100+rm_tolerance}"
                cut_dim2 = f"{l2+10+rm_tolerance}" if l2 else "-"
                length1, length2 = l1 + 10 + 100 + rm_tolerance, l2 + 10 + rm_tolerance if l2 else 0

            cut_dim = f"{cut_dim1},{cut_dim2}" if is_corner else cut_dim1
            length = f"{length1},{length2}" if is_corner else cut_dim1
            
            rm1 = None
            if (a <= 50 and b == 100) or (b <= 50 and a == 100):
                rm1 = "J SEC"
                raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                frappe.log_error(message=f"J {'Corner' if is_corner else 'Straight'}: A={a}, B={b}, using J SEC, Cut={cut_dim}", title="J Section Logic")
            else:
                for (min_a, max_a), (temp_rm1, temp_rm2) in j_sections.items():
                    if min_a <= a <= max_a:
                        rm1 = temp_rm1
                        if rm1 == "AL SHEET":
                            rm_dim = f"{a+65+rm_tolerance}X{l1+rm_tolerance}X4,{a+65+rm_tolerance}X{l2+rm_tolerance}X4" if is_corner else f"{a+65+rm_tolerance}X{l1+rm_tolerance}X4"
                            length = f"{a+65+rm_tolerance}X{l1+rm_tolerance}X4,{a+65+rm_tolerance}X{l2+rm_tolerance}X4" if is_corner else f"{a+65+rm_tolerance}X{l1+rm_tolerance}X4"
                        else:
                            rm_dim = cut_dim
                        raw_materials.append({"code": rm1, "dimension": rm_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        frappe.log_error(message=f"J {'Corner' if is_corner else 'Straight'}: A={a}, using RM1={rm1}, Cut={rm_dim}", title="J Section Logic")
                        break
            
            # Add RM2 if applicable
            if rm1 and rm1 != "J SEC":
                for (min_b, max_b), rm2_code in j_l_sections.items():
                    if min_b <= b <= max_b:
                        raw_materials.append({"code": rm2_code, "dimension": cut_dim, "remark": "J SECTION", "quantity": 1, "length": length})
                        frappe.log_error(message=f"J {'Corner' if is_corner else 'Straight'}: B={b}, using RM2={rm2_code}, Cut={cut_dim}", title="J Section Logic")
                        break
            
            # Determine if made with J
            is_made_with_j = any(rm["code"] == "J SEC" for rm in raw_materials)
            
            # B side rail for SX,SXC,SXCE
            if fg_code_part in ["SX", "SXC", "SXCE","SXCY","SXCZ"]:
                side_rail_qty = 2
                side_rail_dim = f"{b-16+child_tolerance}" if is_made_with_j else f"{b-12+child_tolerance}"
                child_parts.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
                raw_materials.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
            
            # Stiffner plate for JL,JLB,JLT,JLX,JR,JRB,JRT,JRX
            if fg_code_part in ["JL", "JLB", "JLT", "JLX", "JR", "JRB", "JRT", "JRX"]:
                stiff_qty = 2  # Assuming 2, as per previous logic for non-SX
                stiff_dim = f"{a-4+child_tolerance}X{b-16+child_tolerance}X4" if is_made_with_j else f"{a-4+child_tolerance}X{b-12+child_tolerance}X4"
                child_parts.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})
                raw_materials.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})
            
            # Stiffner plate for SX
            if fg_code_part == "SX":
                stiff_qty = 5
                stiff_dim = f"{a-4+child_tolerance}X{b-16+child_tolerance}X4" if is_made_with_j else f"{a-4+child_tolerance}X{b-12+child_tolerance}X4"
                child_parts.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})
                raw_materials.append({"code": "STIFF PLATE", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": stiff_qty, "length": stiff_dim})

            # Outer cap for JL,JLB,JR,JRB
            if fg_code_part in ["JL", "JLB", "JR", "JRB"]:
                outer_cap_dim = f"{a+65+child_tolerance}X{b+child_tolerance}X4"
                child_parts.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})
                raw_materials.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 1, "length": outer_cap_dim})

             # Outer cap for "JLT", "JLX", "JRT", "JRX"
            if fg_code_part in ["JLT", "JLX", "JRT", "JRX"]:
                outer_cap_dim = f"{a+65+child_tolerance}X{b+child_tolerance}X4"
                child_parts.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 2, "length": outer_cap_dim})
                raw_materials.append({"code": "OUTER CAP", "dimension": outer_cap_dim, "remark": "CHILD PART", "quantity": 2, "length": outer_cap_dim})


        elif fg_code_part in t_straight:
            # Apply tolerance to ALL main material dimensions (default case)
            cut_dim = f"{l1+rm_tolerance}"
            length = f"{l1+rm_tolerance}"
            
            # Special cases that override the default
            if fg_code_part in ["WRBSE", "WRSE"] and not degree_cutting:
                cut_dim = f"{l1-50+rm_tolerance}"
                length = f"{l1-50+rm_tolerance}"
            
            for key, (rm1, rm2, rm3) in t_sections.items():
                if isinstance(key, tuple) and len(key) == 2:
                    if key[0] <= a <= key[1]:
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        if rm2 != "-":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        if rm3 != "-":
                            raw_materials.append({"code": rm3, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 2 if rm3 == "EC" else 1, "length": length})
                        frappe.log_error(message=f"T Straight: A={a}, using RM1={rm1}, RM2={rm2}, RM3={rm3}, Cut={cut_dim}", title="T Section Logic")
                        break
                elif isinstance(key, tuple) and len(key) == 1:
                    if a == key[0]:
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        if rm2 != "-":
                            raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 2 if rm2 == "EC" else 1, "length": length})
                        if rm3 != "-":
                            raw_materials.append({"code": rm3, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 2 if rm3 == "EC" else 1, "length": length})
                        frappe.log_error(message=f"T Straight: A={a}, using RM1={rm1}, RM2={rm2}, RM3={rm3}, Cut={cut_dim}", title="T Section Logic")
                        break
            
            side_rail_qty = 2 if degree_cutting and fg_code_part in ["WRBSE", "WRSE"] else 2
            side_rail_dim = f"{a-131+child_tolerance}" if any(rm["code"].endswith("T") for rm in raw_materials) else f"{a-146+child_tolerance}"
            if fg_code_part in ["TSE", "WRBSE", "WRSE"]:
                side_rail_dim = f"{a-131+child_tolerance}" if any(rm["code"].endswith("T") or rm["code"].endswith("CH") for rm in raw_materials) else f"{a-146+child_tolerance}"
            
            child_parts.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
            raw_materials.append({"code": "SIDE RAIL", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": side_rail_qty, "length": side_rail_dim})
            
            if fg_code_part in ["WRBSE", "WRSE"] and not degree_cutting:
                child_parts.append({"code": "RK-50", "dimension": f"{a-130+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{a-130+child_tolerance}"})
                raw_materials.append({"code": "RK-50", "dimension": f"{a-130+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{a-130+child_tolerance}"})
            
            if fg_code_part in ["TSE", "WRBSE", "WRSE", "WSE", "WXSE"]:
                stiff_dim = side_rail_dim
                u_stiff_qty = 5 if fg_code_part == "TSE" else 8
                child_parts.append({"code": "U STIFFNER", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": u_stiff_qty, "length": stiff_dim})
                raw_materials.append({"code": "U STIFFNER", "dimension": stiff_dim, "remark": "CHILD PART", "quantity": u_stiff_qty, "length": stiff_dim})
            
            if fg_code_part in ["WRBSE", "WRSE", "WSE", "WXSE"]:
                child_parts.append({"code": "H STIFFNER", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": 2, "length": side_rail_dim})
                raw_materials.append({"code": "H STIFFNER", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": 2, "length": side_rail_dim})
            
            if fg_code_part in ["PCE", "SBE"]:
                child_parts.append({"code": "I STIFFNER", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": 8, "length": side_rail_dim})
                raw_materials.append({"code": "I STIFFNER", "dimension": side_rail_dim, "remark": "CHILD PART", "quantity": 8, "length": side_rail_dim})
        
        elif fg_code_part in misc_straight:
            # Apply tolerance to ALL main material dimensions (default case)
            # Special handling for MB section which has specific length additions
            if fg_code_part == "MB" and a == 150:
                cut_dim = f"{l1 + 150 + rm_tolerance}"
            elif fg_code_part == "MB" and a == 100:
                cut_dim = f"{l1 + 100 + rm_tolerance}"
            else:
                cut_dim = f"{l1 + rm_tolerance}"
            length = cut_dim
            key = (a, fg_code_part) if fg_code_part in ["EB", "MB", "DP", "RK"] else (a,)
            if key in misc_sections:
                rm1, rm2 = misc_sections[key]
                
                # Special case: RK-125 - check stock availability, fall back to 130 L if not available
                if fg_code_part == "RK" and a == 125:
                    if check_stock_available("RK-125"):
                        # Stock available, use RK-125
                        raw_materials.append({"code": "RK-125", "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        frappe.log_error(message=f"Misc Straight: A={a}, FG={fg_code_part}, using RK-125 (stock available), Cut={cut_dim}", title="Misc Section Logic")
                    else:
                        # Stock not available, use 130 L instead
                        raw_materials.append({"code": "130 L", "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                        frappe.log_error(message=f"Misc Straight: A={a}, FG={fg_code_part}, RK-125 not in stock, using 130 L instead, Cut={cut_dim}", title="Misc Section Logic")
                else:
                    # For all other cases, use normal logic
                    raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                    if rm2 != "-":
                        raw_materials.append({"code": rm2, "dimension": cut_dim, "remark": fg_section_map[fg_code_part], "quantity": 1, "length": length})
                    frappe.log_error(message=f"Misc Straight: A={a}, FG={fg_code_part}, using RM1={rm1}, Cut={cut_dim}", title="Misc Section Logic")
            elif fg_code_part == "RK" and a != 50:
                for (min_a, max_a), (rm1, rm2) in ic_l_section_map.items():
                    if min_a <= a <= max_a:
                        raw_materials.append({"code": rm1, "dimension": cut_dim, "remark": "MISC SECTION", "quantity": 1, "length": length})
                        frappe.log_error(message=f"Misc Straight: RK with A={a}, using RM1={rm1}, Cut={cut_dim}, Remark=MISC SECTION", title="Misc Section Logic")
                        break
            
            if fg_code_part == "DP":
                child_parts.append({"code": "ROUND PIPE", "dimension": f"{196+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{196+child_tolerance}"})
                raw_materials.append({"code": "ROUND PIPE", "dimension": f"{196+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{196+child_tolerance}"})
            
            if a == 150:
                child_parts.append({"code": "ROUND PIPE", "dimension": f"{150+child_tolerance}X{150+child_tolerance}X4", "remark": "CHILD PART", "quantity": 1, "length": f"{150+child_tolerance}X{150+child_tolerance}X4"})
                raw_materials.append({"code": "ROUND PIPE", "dimension": f"{150+child_tolerance}X{150+child_tolerance}X4", "remark": "CHILD PART", "quantity": 1, "length": f"{150+child_tolerance}X{150+child_tolerance}X4"})
            if fg_code_part in ["EB", "MB"] and a == 150:
                child_parts.append({"code": "ROUND PIPE", "dimension": f"{105+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{105+child_tolerance}"})
                raw_materials.append({"code": "ROUND PIPE", "dimension": f"{105+child_tolerance}", "remark": "CHILD PART", "quantity": 1, "length": f"{105+child_tolerance}"})

        else:
            frappe.log_error(message=f"Skipping invalid FG Code: {fg_code_part}, Full Code: {fg_code}", title="FG Raw Material Error")
            return []
        
        frappe.log_error(message=f"Child Parts for FG Code {fg_code_part}: {json.dumps(child_parts, indent=2)}", title="Child Parts Debug")
        frappe.log_error(message=f"Returning raw_materials for FG Code {fg_code}: {json.dumps(raw_materials, indent=2)}", title="FG Single Code Debug")
        return raw_materials



@frappe.whitelist()
def get_raw_materials(docname=None, planning_bom=None):
    try:
        frappe.log_error(
            message=f"get_raw_materials called with docname: {docname}, planning_bom: {planning_bom}",
            title="FG Get Raw Materials Debug"
        )
        if not docname:
            frappe.throw(_("No FG Raw Material Selector document specified."))
        if not planning_bom:
            frappe.throw(_("No Planning BOMs provided."))

        doc = frappe.get_doc("FG Raw Material Selector", docname)
        
        # Auto-set project from first Planning BOM
        if doc.planning_bom and len(doc.planning_bom) > 0:
            first_pbom_name = doc.planning_bom[0].planning_bom
            try:
                first_pbom = frappe.get_doc("Planning BOM", first_pbom_name)
                if first_pbom.project:
                    doc.project = first_pbom.project
                    doc.save()
            except Exception as e:
                frappe.log_error(f"Error fetching project from Planning BOM: {str(e)}", "FG Auto-fetch Project")

        pbom_list = []
        if isinstance(planning_bom, str):
            try:
                pbom_data = json.loads(planning_bom)
                if isinstance(pbom_data, list):
                    for entry in pbom_data:
                        if isinstance(entry, dict):
                            value = entry.get("planning_bom") or entry.get("value") or entry.get("name")
                            if value:
                                pbom_list.append(value)
                        elif isinstance(entry, str):
                            pbom_list.append(entry)
                elif isinstance(pbom_data, str):
                    pbom_list = [pbom_data]
                elif isinstance(pbom_data, dict):
                    value = pbom_data.get("planning_bom") or pbom_data.get("value") or pbom_data.get("name")
                    if value:
                        pbom_list.append(value)
            except json.JSONDecodeError:
                pbom_list = [planning_bom]
        elif isinstance(planning_bom, list):
            for pb in planning_bom:
                if isinstance(pb, str):
                    pbom_list.append(pb)

        pbom_list = [p for p in pbom_list if p]
        if not pbom_list:
            frappe.throw(_("No valid Planning BOMs found."))

        # doc is already fetched above for project auto-fetch
        existing = {row.planning_bom for row in doc.planning_bom}
        for pb in pbom_list:
            if pb not in existing:
                doc.append("raw_materials", {"planning_bom": pb})

        doc.save()

        job_id = frappe.enqueue(
            'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.process_fg_codes_background',
            queue='long',
            timeout=3600,
            docname=doc.name
        )

        frappe.msgprint(
            _("Raw material processing has been queued (Job ID: {0}).").format(job_id)
        )
        return []

    except Exception as e:
        frappe.throw(_("Failed to fetch raw materials: {0}").format(str(e)))
@frappe.whitelist()
def process_fg_codes_background(docname):
    try:
        frappe.log_error(message=f"Starting background job for FG Raw Material Selector: {docname}", title="FG Background Debug")
        doc = frappe.get_doc("FG Raw Material Selector", docname)
        doc.process_fg_codes()
        frappe.log_error(message=f"Completed background job for FG Raw Material Selector: {docname}", title="FG Background Debug")
    except Exception as e:
        frappe.log_error(message=f"Error in background job for {docname}: {str(e)}", title="FG Raw Material Error")
        raise


@frappe.whitelist()
def create_material_request(fg_selector_name):
    try:
        doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)

        groups = defaultdict(list)
        for row in doc.raw_materials:
            if row.status == "NIS":
                groups[row.item_code].append(row)

        if not groups:
            frappe.throw("No NIS items to create Material Request for.")

        mr = frappe.new_doc("Material Request")
        mr.material_request_type = "Purchase"
        mr.transaction_date = frappe.utils.nowdate()

        for item_code, group_rows in groups.items():
            total_length = 0.0
            total_piece_qty = 0.0
            uom = group_rows[0].uom if group_rows[0].uom else "Nos"
            
            # Get warehouse from row or parent document
            warehouse = None
            for row in group_rows:
                if row.warehouse:
                    warehouse = row.warehouse
                    break
            
            # If no warehouse in rows, use parent document's raw_material_warehouse
            if not warehouse:
                warehouse = getattr(doc, 'raw_material_warehouse', None) or getattr(doc, 'offcut_warehouse', None)
            
            # If still no warehouse, throw error
            if not warehouse:
                frappe.throw(f"Warehouse is required for item {item_code}. Please set warehouse in raw materials table or in parent document.")

            for row in group_rows:
                dims = []
                if row.dimension:
                    dims += [flt(v.strip()) for v in row.dimension.split(',') if v.strip()]
                if dims:
                    total_length += sum(dims) * flt(row.quantity)
                else:
                    total_piece_qty += flt(row.quantity)

            if total_length > 0:
                required = math.ceil(total_length / 4820)
            else:
                required = math.ceil(total_piece_qty)

            available = group_rows[0].available_quantity

            shortfall = required - available if available < required else 0

            if shortfall > 0:
                mr.append("items", {
                    "item_code": item_code,
                    "qty": shortfall,
                    "uom": uom,
                    "warehouse": warehouse,
                    "schedule_date": frappe.utils.nowdate(),
                    "description": f"Consolidated shortfall for NIS item {item_code} from FG Raw Material Selector {fg_selector_name}. Linked reserve tags: {', '.join([str(row.name) for row in group_rows])}",
                })

        if mr.items:
            mr.insert(ignore_permissions=True)
            frappe.db.commit()
            return mr.name
        else:
            return "No shortfalls to request."

    except Exception as e:
        frappe.log_error(message=f"Error creating Material Request: {str(e)}", title="FG Raw Material Error")
        

@frappe.whitelist()
def mark_as_completed(docname):
    """Mark FG Raw Material Selector as Completed"""
    try:
        doc = frappe.get_doc("FG Raw Material Selector", docname)
        doc.status = "Completed"
        doc.save()
        frappe.db.commit()
        return {
            "status": "success",
            "message": f"FG Raw Material Selector {docname} marked as Completed"
        }
    except Exception as e:
        frappe.log_error(message=f"Error marking as completed: {str(e)}", title="FG Raw Material Error")
        frappe.throw(f"Failed to mark as completed: {str(e)}")

@frappe.whitelist()
def cancel_document(docname):
    """Cancel FG Raw Material Selector"""
    try:
        doc = frappe.get_doc("FG Raw Material Selector", docname)
        doc.status = "Cancelled"
        doc.save()
        frappe.db.commit()
        return {
            "status": "success",
            "message": f"FG Raw Material Selector {docname} cancelled"
        }
    except Exception as e:
        frappe.log_error(message=f"Error cancelling document: {str(e)}", title="FG Raw Material Error")
        frappe.throw(f"Failed to cancel document: {str(e)}")

@frappe.whitelist()
def fetch_pending_items(docname):
    """Fetch pending items (remaining_qty > 0) from selected Planning BOMs"""
    try:
        doc = frappe.get_doc("FG Raw Material Selector", docname)
        
        if not doc.planning_bom:
            frappe.throw("Please select at least one Planning BOM")
        
        # Auto-set project from first selected Planning BOM
        if doc.planning_bom and len(doc.planning_bom) > 0:
            first_pbom_name = doc.planning_bom[0].planning_bom
            first_pbom = frappe.get_doc("Planning BOM", first_pbom_name)
            if first_pbom.project:
                doc.project = first_pbom.project
        
        # Clear existing raw materials
        doc.set("raw_materials", [])
        
        item_count = 0
        # Fetch pending items from each Planning BOM
        for pbom_row in doc.planning_bom:
            pbom_name = pbom_row.planning_bom
            pbom_doc = frappe.get_doc("Planning BOM", pbom_name)
            
            for item in pbom_doc.items:
                remaining = flt(item.remaining_qty)
                
                # Only add items with remaining quantity > 0
                if remaining > 0:
                    doc.append("raw_materials", {
                        "fg_code": item.fg_code,
                        "item_code": item.item_code,
                        "dimension": item.dimension,
                        "quantity": remaining,  # Default to full remaining quantity
                        "remark": item.remark,
                        "project": item.project,
                        "ipo_name": item.ipo_name,
                        "dwg_no": item.dwg_no,
                        "uom": item.uom,
                        "planning_bom": pbom_name,
                        "planning_bom_item_reference": item.name,
                        "a": item.a,
                        "b": item.b,
                        "code": item.code,
                        "l1": item.l1,
                        "l2": item.l2,
                        "bom_qty": item.quantity,
                        "u_area": item.u_area
                    })
                    item_count += 1
        
        doc.save()
        
        return {
            "status": "success",
            "message": f"Fetched {item_count} pending items from {len(doc.planning_bom)} Planning BOM(s)"
        }
    except Exception as e:
        frappe.log_error(message=f"Error fetching pending items: {str(e)}", title="FG Raw Material Error")
        frappe.throw(f"Failed to fetch pending items: {str(e)}")


def _allocate_rm_oc_pieces_for_reservation(doc):
    """
    Reservation-only RM/OC allocation.

    Runs the same allocation logic as `_run_rm_oc_calculation`, but:
    - does NOT write to the `rm_oc_simulation` child table (avoids 20k+ DB rows)
    - only returns the set of OC/RM Serial No pieces that must be reserved
    - updates `doc.raw_materials[*].status` to IS/NIS based on whether any cut
      could not be allocated (same meaning as NS rows in simulation mode).
    """

    oc_warehouse = doc.offcut_warehouse or "OC - Trial - Sbs"
    rm_warehouse = doc.raw_material_warehouse or "RM - Trial - Sbs"
    min_oc_length_mm = 200  # if balance < this, it is treated as scrap (only affects expected stock in simulation)

    if not hasattr(doc, "raw_materials"):
        frappe.throw("Document is missing child table 'raw_materials'")

    # STEP 1: Collect all cuts item-wise + total qty for each (FG, RM)
    cuts_by_item = {}
    total_qty_map = {}  # key: (fg, rm_item, length) -> total qty

    for row in doc.raw_materials:
        if not row.item_code or not row.dimension or not row.quantity:
            continue

        fg_code = row.fg_code or ""
        rm_item = row.item_code
        qty = cint(row.quantity)

        required_lengths = [
            flt(v.strip())
            for v in (row.dimension or "").split(",")
            if v.strip() and v.strip() != "-"
        ]

        for length in required_lengths:
            key = (fg_code, rm_item, length)
            total_qty_map[key] = total_qty_map.get(key, 0) + qty

    for (fg_code, rm_item, cut_length), total_qty in total_qty_map.items():
        cuts_by_item.setdefault(rm_item, [])
        for _ in range(total_qty):
            cuts_by_item[rm_item].append(
                {
                    "fg_code": fg_code,
                    "rm_item_code": rm_item,
                    "length": cut_length,
                }
            )

    if not cuts_by_item:
        frappe.throw("No valid raw materials found on this document")

    reserved_serial_nos = set()
    # serial_no -> {s_warehouse, source_type}
    piece_map = {}
    fg_has_ns = set()
    # rm_item_code -> {cut_length_mm -> qty_of_rm}
    ns_length_qty = defaultdict(lambda: defaultdict(int))
    # rm_item_code -> set(fg_code)
    ns_fg_links = defaultdict(set)

    def assign_from_single_piece_no_save(piece_dict, cuts_list, source_type: str):
        """
        Allocate from one OC/RM Serial No piece to as many cuts as fit.
        Mutates `cuts_list` in-place (pops allocated cuts).
        """

        remaining_length = flt(piece_dict.get("current_length_mm") or 0)
        allocated_any = False

        # cuts_list expected sorted by length DESC
        while True:
            candidate_index = None

            for idx, cut in enumerate(cuts_list):
                required_len = flt(cut["length"])

                # Apply tolerance only for the first cut allocated to this piece (RM mode)
                if source_type == "RM" and not allocated_any:
                    required_len += 4

                if required_len <= remaining_length:
                    candidate_index = idx
                    break

            if candidate_index is None:
                break  # no more cuts fit in this piece

            cut = cuts_list.pop(candidate_index)

            consumed_len = flt(cut["length"])
            if source_type == "RM" and not allocated_any:
                consumed_len += 4

            remaining_length = remaining_length - consumed_len

            # First successful allocation for this piece => piece must be reserved
            if not allocated_any:
                allocated_any = True
                sn = piece_dict["name"]
                reserved_serial_nos.add(sn)
                piece_map[sn] = {
                    "source_type": source_type,
                    "s_warehouse": oc_warehouse if source_type == "OC" else rm_warehouse,
                    "item_code": cut.get("rm_item_code"),
                }

        piece_dict["current_length_mm"] = remaining_length

    # STEP 2: Process each RM item independently
    for rm_item, cuts in cuts_by_item.items():
        if not cuts:
            continue

        # Sort cuts by length DESC so each piece uses largest cuts first
        cuts.sort(key=lambda x: x["length"], reverse=True)

        # Load OC pieces of this RM item from OC warehouse, shortest first
        oc_pieces = frappe.get_all(
            "Serial No",
            filters={
                "item_code": rm_item,
                "warehouse": oc_warehouse,
                "custom_length": [">", 0],
            },
            fields=["serial_no as name", "custom_length as current_length_mm"],
        )
        oc_pieces.sort(key=lambda x: flt(x.get("current_length_mm") or 0))

        # Load RM pieces of this RM item from RM warehouse, shortest first
        rm_pieces = frappe.get_all(
            "Serial No",
            filters={
                "item_code": rm_item,
                "warehouse": rm_warehouse,
                "custom_length": [">", 0],
            },
            fields=["serial_no as name", "custom_length as current_length_mm"],
        )
        rm_pieces.sort(key=lambda x: flt(x.get("current_length_mm") or 0))

        # FIRST: Allocate from OC pieces (shortest first)
        for oc in oc_pieces:
            if not cuts:
                break
            assign_from_single_piece_no_save(oc, cuts, "OC")

        # SECOND: Allocate remaining cuts from RM pieces (shortest first)
        for bar in rm_pieces:
            if not cuts:
                break
            assign_from_single_piece_no_save(bar, cuts, "RM")

        # THIRD: Anything remaining is NOT IN STOCK (NS)
        # Any remaining cut => its FG must be marked NIS
        if cuts:
            for cut in cuts:
                fg_has_ns.add(cut["fg_code"])
                ns_length_qty[rm_item][flt(cut["length"])] += 1
                ns_fg_links[rm_item].add(cut["fg_code"])

    # STEP 3: Update status in raw_materials based on allocation.
    # Primary key is FG code, with item-code fallback in case FG mapping is
    # incomplete/inconsistent for some generated rows.
    ns_item_codes = set(ns_length_qty.keys())
    for raw_row in doc.raw_materials:
        fg_code = (raw_row.fg_code or "").strip()
        item_code = (raw_row.item_code or "").strip()
        raw_row.status = "NIS" if (fg_code in fg_has_ns or item_code in ns_item_codes) else "IS"

    # Keep `min_oc_length_mm` referenced so it won't look unused during linting
    _ = min_oc_length_mm

    # Build NS shortfall details for Material Request creation:
    # rm_item_code -> {"details":[(length,qty)], "fg_links":[...]}
    ns_shortfalls = {}
    for rm_item_code, length_map in ns_length_qty.items():
        details = [(length, qty) for length, qty in length_map.items() if length > 0 and qty > 0]
        details.sort(key=lambda x: x[0], reverse=True)
        ns_shortfalls[rm_item_code] = {
            "details": details,
            "fg_links": sorted(ns_fg_links.get(rm_item_code, set())),
        }

    # Return serial numbers to reserve + warehouse mapping + NS shortfalls
    return reserved_serial_nos, piece_map, ns_shortfalls


def _run_rm_oc_calculation(fg_selector_name: str) -> str:
    """
    Run RM / OC calculator (sync). Used by background worker for large datasets
    to avoid HTTP / Gunicorn timeouts.
    """
    if not fg_selector_name:
        frappe.throw("FG Raw Material Selector name is required")

    doc = frappe.get_doc("FG Raw Material Selector", fg_selector_name)

    oc_warehouse = doc.offcut_warehouse or "OC - Trial - Sbs"
    rm_warehouse = doc.raw_material_warehouse or "RM - Trial - Sbs"
    min_oc_length_mm = 200  # if balance < this, it is treated as scrap

    if not hasattr(doc, "raw_materials"):
        frappe.throw("Document is missing child table 'raw_materials'")

    # After removing the `rm_oc_simulation` table field from this DocType,
    # fall back to reservation-only allocation (no simulation rows are saved).
    if not hasattr(doc, "rm_oc_simulation"):
        _reserved_serial_nos, _piece_map, _ns_shortfalls = _allocate_rm_oc_pieces_for_reservation(doc)
        frappe.db.connect()
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return f"RM / OC calculation completed for FG Raw Material Selector {fg_selector_name} (no simulation table saved)"

    # STEP 1: Collect all cuts item-wise + total qty for each (FG, RM)
    cuts_by_item = {}
    total_qty_map = {}  # key: (fg, rm_item, length) -> total qty

    for row in doc.raw_materials:
        if not row.item_code or not row.dimension or not row.quantity:
            continue

        fg_code = row.fg_code or ""
        rm_item = row.item_code
        qty = cint(row.quantity)

        required_lengths = [flt(v.strip()) for v in (row.dimension or "").split(',') if v.strip() and v.strip() != '-']

        for length in required_lengths:
            key = (fg_code, rm_item, length)
            total_qty_map[key] = total_qty_map.get(key, 0) + qty

    for (fg_code, rm_item, cut_length), total_qty in total_qty_map.items():
        cuts_by_item.setdefault(rm_item, [])
        rm_code_display = f"{rm_item}-{int(cut_length)}"  # e.g. C-125-1250

        for _ in range(total_qty):
            cuts_by_item[rm_item].append({
                "fg_code": fg_code,
                "rm_item_code": rm_item,
                "length": cut_length,
                "rm_code_display": rm_code_display,
                "qty_total_for_fg_rm": total_qty
            })

    if not cuts_by_item:
        frappe.throw("No valid raw materials found on this document")

    sim_rows = []

    # Helper: allocate cuts from a single piece (OC or RM)
    def assign_from_single_piece(piece_dict, cuts_list, source_type: str):
        allocations_for_piece = []
        remaining_length = flt(piece_dict["current_length_mm"] or 0)

        # cuts_list expected sorted by length DESC
        while True:
            candidate_index = None

            for idx, cut in enumerate(cuts_list):
                # Calculate required length including tolerance for first cut of RM
                required_len = flt(cut["length"])
                is_first_rm_cut = (source_type == "RM" and len(allocations_for_piece) == 0)
                
                if is_first_rm_cut:
                    required_len += 4

                if required_len <= remaining_length:
                    candidate_index = idx
                    break

            if candidate_index is None:
                break  # no more cuts fit in this piece

            cut = cuts_list.pop(candidate_index)
            
            # Calculate actual consumption
            consumed_len = flt(cut["length"])
            if source_type == "RM" and len(allocations_for_piece) == 0:
                 consumed_len += 4
                 # Update display code to reflect the actual cutting length (including tolerance)
                 cut["rm_code_display"] = f"{cut['rm_item_code']}-{int(consumed_len)}"

            before = remaining_length
            after = before - consumed_len

            scrap_length = after if after < min_oc_length_mm and after > 0 else 0
            oc_balance = after if after >= min_oc_length_mm else 0

            allocations_for_piece.append({
                "fg_code": cut["fg_code"],
                "rm_item_code": cut["rm_item_code"],
                "rm_code_display": cut["rm_code_display"],      # Excel RM Code -> Cutting Code
                "qty_of_rm": 1,                                 # Each row is 1 piece
                "source_type": source_type,                     # OC / RM
                "source_status": "IS",                          # In Stock
                "ns_flag": 0,
                "open_stock_oc_reservation": piece_dict["name"] if source_type == "OC" else None,
                "open_stock_rm_reservation": piece_dict["name"] if source_type == "RM" else None,
                "remaining_temp_in_oc_wh": after,
                "expected_oc_stock": oc_balance,
                "expected_scrap_stock": scrap_length,
            })

            remaining_length = after

        piece_dict["current_length_mm"] = remaining_length
        return allocations_for_piece

    # STEP 2: Process each RM item independently
    for rm_item, cuts in cuts_by_item.items():
        if not cuts:
            continue

        # Sort cuts by length DESC so each piece uses largest cuts first
        cuts.sort(key=lambda x: x["length"], reverse=True)

        # Load OC pieces of this RM item from OC warehouse, shortest first
        oc_pieces = frappe.get_all(
            "Serial No",
            filters={
                "item_code": rm_item,
                "warehouse": oc_warehouse,
                "custom_length": [">", 0]
            },
            fields=["serial_no as name", "custom_length as current_length_mm"],
        )
        oc_pieces.sort(key=lambda x: flt(x["current_length_mm"] or 0))

        # Load RM pieces of this RM item from RM warehouse, shortest first
        rm_pieces = frappe.get_all(
            "Serial No",
            filters={
                "item_code": rm_item,
                "warehouse": rm_warehouse,
                "custom_length": [">", 0]
            },
            fields=["serial_no as name", "custom_length as current_length_mm"],
        )
        rm_pieces.sort(key=lambda x: flt(x["current_length_mm"] or 0))

        # FIRST: Allocate from OC pieces (shortest first)
        for oc in oc_pieces:
            if not cuts:
                break
            sim_rows.extend(assign_from_single_piece(oc, cuts, "OC"))

        # SECOND: Allocate remaining cuts from RM pieces (shortest first)
        for bar in rm_pieces:
            if not cuts:
                break
            sim_rows.extend(assign_from_single_piece(bar, cuts, "RM"))

        # THIRD: Anything remaining is NOT IN STOCK (NS)
        for cut in cuts:
            sim_rows.append({
                "fg_code": cut["fg_code"],
                "rm_item_code": cut["rm_item_code"],
                "rm_code_display": cut["rm_code_display"],
                "qty_of_rm": 1,
                "source_type": "NS",
                "source_status": "NS",
                "ns_flag": 1,
                "open_stock_oc_reservation": None,
                "open_stock_rm_reservation": None,
                "remaining_temp_in_oc_wh": 0,
                "expected_oc_stock": 0,
                "expected_scrap_stock": 0,
            })

    # STEP 3: Write results into RM OC Simulation child table
    if not hasattr(doc, "rm_oc_simulation"):
        frappe.throw("Document is missing child table 'rm_oc_simulation'")

    doc.set("rm_oc_simulation", [])

    for r in sim_rows:
        row = doc.append("rm_oc_simulation", {})
        # Excel columns
        row.fg_code = r.get("fg_code")
        row.rm_item_code = r.get("rm_item_code")
        row.cutting_code = r.get("rm_code_display")                 # "C-125-1250" style

        row.qty_of_rm = r.get("qty_of_rm")
        row.open_stock_rm_reservation = r.get("open_stock_rm_reservation")
        row.open_stock_oc_reservation = r.get("open_stock_oc_reservation")
        row.remaining_temp_in_oc_wh = r.get("remaining_temp_in_oc_wh")
        row.expected_oc_stock = r.get("expected_oc_stock")
        row.expected_scrap_stock = r.get("expected_scrap_stock")
        # Internal flags
        row.source_type = r.get("source_type")
        row.source_status = r.get("source_status")
        row.ns_flag = r.get("ns_flag")

    # STEP 4: Update status in raw_materials based on simulation (all cuts covered = IS; any NS = NIS)
    for raw_row in doc.raw_materials:
        fg = raw_row.fg_code
        has_ns = any(r["ns_flag"] for r in sim_rows if r["fg_code"] == fg)
        raw_row.status = "NIS" if has_ns else "IS"

    # Reconnect to database (long-running jobs may have stale connections)
    frappe.db.connect()
    
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return f"RM / OC calculation completed for FG Raw Material Selector {fg_selector_name}"


def rm_oc_calculator_background(fg_selector_name, user):
    """Run RM/OC in a worker (long timeout). Notifies the user via realtime when done."""
    # Wrap everything in try/catch to handle errors at any point
    error_occurred = False
    error_msg = None
    
    try:
        # Ensure fresh DB connection (long-running context)
        frappe.db.connect()
        
        if not fg_selector_name:
            raise ValueError("FG Raw Material Selector name is required")
        
        msg = _run_rm_oc_calculation(fg_selector_name)
        
        frappe.publish_realtime(
            event="rm_oc_calculation_done",
            message={
                "status": "success",
                "message": msg,
                "docname": fg_selector_name,
            },
            user=user,
        )
    except Exception as e:
        error_occurred = True
        error_msg = str(e) or "Unknown error occurred"
        traceback_msg = frappe.get_traceback()
        
        # Try to log error (with fresh connection in case that's the issue)
        try:
            frappe.db.connect()
            frappe.log_error(
                message=traceback_msg,
                title=f"RM/OC Calculator Error - {fg_selector_name or 'Unknown'}",
            )
        except Exception as log_err:
            print(f"Failed to log error: {log_err}")
        
        # Always send notification, even if logging fails
        try:
            frappe.publish_realtime(
                event="rm_oc_calculation_done",
                message={
                    "status": "error",
                    "message": error_msg,
                    "docname": fg_selector_name or "Unknown",
                },
                user=user,
            )
        except Exception as rt_err:
            print(f"Failed to publish realtime: {rt_err}, original error: {error_msg}")


@frappe.whitelist()
def rm_oc_calculator(fg_selector_name: str):
    """
    Queue RM / OC calculation on the long worker queue.

    Output structure (RM OC Simulation child table) is aligned to Excel:

    FG Code
    RM Code
    Qty of RM
    Open_Stock RM Reservation
    Open_Stock OC Reservation
    Remaining Temp in OC WH
    Expected OC Stock
    Expected Scrap Stock

    Plus internal fields: source_type, source_status, ns_flag.
    """
    if not fg_selector_name:
        frappe.throw("FG Raw Material Selector name is required")

    job = frappe.enqueue(
        "sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.rm_oc_calculator_background",
        queue="long",
        timeout=7200,
        fg_selector_name=fg_selector_name,
        user=frappe.session.user,
    )

    return {
        "status": "queued",
        "message": _("RM/OC calculation has been queued (Job ID: {0}). You will be notified when it completes.").format(
            job.id
        ),
        "job_id": job.id,
    }