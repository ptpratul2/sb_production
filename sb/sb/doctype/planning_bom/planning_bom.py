# planning_bom.py
# Copyright (c) 2025, ptpratul2@gmail.com and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class PlanningBOM(Document):
    def before_save(self):
        """Set default status and calculate quantities"""
        if not self.status:
            self.status = "Draft"
        
        # Calculate remaining quantities for all items
        self.calculate_remaining_quantities()
        
        # Update status based on processing
        self.update_processing_status()
    
    def validate(self):
        """Validate Planning BOM"""
        if not self.planning_date:
            self.planning_date = frappe.utils.today()
        
        # Prevent modification if fully processed
        if not self.is_new() and self.status == "Fully Processed":
            old_doc = self.get_doc_before_save()
            if old_doc and self.has_value_changed_except_status():
                frappe.throw("Cannot modify fully processed Planning BOM")
    
    def calculate_remaining_quantities(self):
        """Calculate remaining_qty for all items"""
        for item in self.items:
            item.remaining_qty = flt(item.required_qty or item.quantity) - flt(item.processed_qty)
    
    def update_processing_status(self):
        """Update status based on item processing"""
        if not self.items:
            if self.status not in ["Cancelled", "Fully Processed"]:
                self.status = "Draft"
            return
        
        total_required = sum(flt(item.required_qty or item.quantity) for item in self.items)
        total_processed = sum(flt(item.processed_qty) for item in self.items)
        
        if total_required == 0:
            return
        
        if self.status == "Cancelled":
            return  # Don't change cancelled status
        
        if total_processed == 0 and self.status == "Draft":
            # Items added but not processed yet
            self.status = "In Progress"
        elif total_processed > 0 and total_processed < total_required:
            # Partially processed
            self.status = "Partially Processed"
        elif total_processed >= total_required:
            # Fully processed
            self.status = "Fully Processed"
    
    def has_value_changed_except_status(self):
        """Check if any field other than status has changed"""
        if not self.get_doc_before_save():
            return False
        
        old_doc = self.get_doc_before_save()
        exclude_fields = ['status', 'modified', 'modified_by']
        
        for field in self.meta.fields:
            if field.fieldname in exclude_fields:
                continue
            if self.get(field.fieldname) != old_doc.get(field.fieldname):
                return True
        return False


@frappe.whitelist()
def consolidate_project_design_uploads(docname):
    """
    Append all items from selected Project Design Upload documents
    into Planning BOM without consolidation (as-is).
    """
    doc = frappe.get_doc("Planning BOM", docname)
    
    if not doc.project_design_upload:
        frappe.throw("Please select at least one Project Design Upload document")
    
    # Auto-set project from first selected PDU
    if doc.project_design_upload and len(doc.project_design_upload) > 0:
        first_pdu_name = doc.project_design_upload[0].project_design
        first_pdu = frappe.get_doc("Project Design Upload", first_pdu_name)
        if first_pdu.project:
            doc.project = first_pdu.project
    
    # Clear existing items
    doc.set("items", [])
    
    item_count = 0
    
    # Process each selected Project Design Upload
    for row in doc.project_design_upload:
        design_upload_name = row.project_design
        design_upload_doc = frappe.get_doc("Project Design Upload", design_upload_name)
        
        # Update PDU status to "Planned" if it's Open
        if design_upload_doc.processed_status == "Open":
            design_upload_doc.processed_status = "Planned"
            design_upload_doc.save(ignore_permissions=True)
        
        # Directly append each item without consolidation
        for item in design_upload_doc.items:
            required_qty = flt(item.get('quantity', 0))
            
            doc.append("items", {
                'project_design_upload': item.get('parent'),
                'project_design_upload_item': item.get('name'),
                'fg_code': item.get('fg_code'),
                'item_code': item.get('item_code'),
                'dimension': item.get('dimension'),
                'quantity': required_qty,
                'required_qty': required_qty,  # Set required quantity
                'processed_qty': 0,  # Initialize processed quantity
                'remaining_qty': required_qty,  # Initialize remaining quantity
                'remark': item.get('remark'),
                'project': item.get('project'),
                'ipo_name': item.get('ipo_name'),
                'a': item.get('a'),
                'b': item.get('b'),
                'code': item.get('code'),
                'l1': item.get('l1'),
                'l2': item.get('l2'),
                'dwg_no': item.get('dwg_no'),
                'u_area': item.get('u_area'),
                'room_no': item.get('room_no'),
                'flat_no': item.get('flat_no'),
                'uom': item.get('uom'),
                'source_document': design_upload_name
            })
            
            # Update planned_qty in source PDU item
            frappe.db.set_value("Project Design Upload Item", item.name, {
                "planned_qty": required_qty,
                "remaining_qty": required_qty - flt(item.get('processed_qty', 0))
            })
            
            item_count += 1
    
    # Save the document
    doc.save()
    
    return {
        "status": "success",
        "message": f"Successfully appended {item_count} items from {len(doc.project_design_upload)} Project Design Upload documents without consolidation"
    }
@frappe.whitelist()
def get_consolidation_preview(docname):
    """
    Preview all items from selected Project Design Upload documents
    without consolidation (as-is).
    """
    doc = frappe.get_doc("Planning BOM", docname)
    
    if not doc.project_design_upload:
        return {"preview": []}
    
    preview = []
    total_qty = 0
    total_unit_area = 0

    for row in doc.project_design_upload:
        design_upload_doc = frappe.get_doc("Project Design Upload", row.project_design)
        
        for item in design_upload_doc.items:
            quantity = flt(item.get('quantity', 0))
            u_area = flt(item.get('u_area', 0))

            preview.append({
                'fg_code': item.get('fg_code'),
                'item_code': item.get('item_code'),
                'dimension': item.get('dimension'),
                'quantity': quantity,
                'u_area': u_area,  # ✅ include u_area in each row
                'project': item.get('project'),
                'source_document': row.project_design
            })

            total_qty += quantity
            total_unit_area += u_area  # ✅ accumulate while iterating
    
    return {
        "preview": preview,
        "total_items": len(preview),
        "total_quantity": total_qty,
        "total_unit_area": total_unit_area
    }

@frappe.whitelist()
def get_project_design_upload_summary(docname):
    """
    Get summary of selected Project Design Upload documents
    """
    doc = frappe.get_doc("Planning BOM", docname)
    
    if not doc.project_design_upload:
        return {"summary": []}
    
    summary = []
    total_items = 0
    
    for row in doc.project_design_upload:
        design_upload_doc = frappe.get_doc("Project Design Upload", row.project_design)
        item_count = len(design_upload_doc.items)
        total_items += item_count
        
        summary.append({
            "name": row.project_design,
            "project": design_upload_doc.project,
            "upload_date": design_upload_doc.upload_date,
            "item_count": item_count,
            "processed_status": design_upload_doc.processed_status
        })
    
    return {
        "summary": summary,
        "total_documents": len(doc.project_design_upload),
        "total_items": total_items
    }

@frappe.whitelist()
def get_pending_items(docname):
    """Get items with remaining_qty > 0 for FG Raw Material Selector"""
    doc = frappe.get_doc("Planning BOM", docname)
    
    pending_items = []
    for item in doc.items:
        remaining = flt(item.remaining_qty)
        if remaining > 0:
            pending_items.append({
                "planning_bom_item": item.name,
                "fg_code": item.fg_code,
                "item_code": item.item_code,
                "dimension": item.dimension,
                "required_qty": flt(item.required_qty or item.quantity),
                "processed_qty": flt(item.processed_qty),
                "remaining_qty": remaining,
                "project": item.project,
                "uom": item.uom
            })
    
    return pending_items


@frappe.whitelist()
def create_fg_raw_material_selector(planning_bom_name):
    """
    Create a new FG Raw Material Selector with this Planning BOM pre-selected
    """
    try:
        # Validate Planning BOM exists
        if not frappe.db.exists("Planning BOM", planning_bom_name):
            frappe.throw(f"Planning BOM {planning_bom_name} not found")
        
        planning_bom_doc = frappe.get_doc("Planning BOM", planning_bom_name)
        
        # Create new FG Raw Material Selector
        fg_selector = frappe.new_doc("FG Raw Material Selector")
        fg_selector.project = planning_bom_doc.project if planning_bom_doc.project else None
        
        # Add the Planning BOM to the multiselect table
        fg_selector.append("planning_bom", {
            "planning_bom": planning_bom_name
        })
        
        # Save the new FG Raw Material Selector
        fg_selector.insert(ignore_permissions=True)
        frappe.db.commit()
        
        return {
            "status": "success",
            "fg_selector_name": fg_selector.name,
            "message": f"FG Raw Material Selector {fg_selector.name} created with {planning_bom_name} pre-selected"
        }
    except Exception as e:
        frappe.log_error(f"Error creating FG Raw Material Selector: {str(e)}", "Create FG Selector Error")
        frappe.throw(f"Failed to create FG Raw Material Selector: {str(e)}")
