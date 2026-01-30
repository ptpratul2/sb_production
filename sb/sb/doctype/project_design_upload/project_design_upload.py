# Copyright (c) 2025, ptpratul2@gmail.com and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ProjectDesignUpload(Document):
	def before_save(self):
		"""Set default status to Open if not set"""
		if not self.processed_status:
			self.processed_status = "Open"
	
	def validate(self):
		"""Prevent modification when status is not Open"""
		if not self.is_new():
			old_doc = self.get_doc_before_save()
			if old_doc and old_doc.processed_status in ["In Process", "Completed"]:
				# Allow status changes but prevent other field modifications
				if self.has_value_changed_except_status():
					frappe.throw(
						f"Cannot modify Project Design Upload with status '{old_doc.processed_status}'. "
						"Only Open status documents can be modified."
					)
	
	def has_value_changed_except_status(self):
		"""Check if any field other than status has changed"""
		if not self.get_doc_before_save():
			return False
		
		old_doc = self.get_doc_before_save()
		exclude_fields = ['processed_status', 'modified', 'modified_by', 'docstatus']
		
		for field in self.meta.fields:
			if field.fieldname in exclude_fields:
				continue
			if self.get(field.fieldname) != old_doc.get(field.fieldname):
				return True
		return False
	
	def before_submit(self):
		"""Prevent submission if used in Planning BOM or FG Raw Material Selector"""
		if self.processed_status != "Open":
			frappe.throw(
				f"Cannot submit Project Design Upload with status '{self.processed_status}'. "
				"Only Open status documents can be submitted."
			)


import pandas as pd
from frappe.utils.file_manager import get_file_path

@frappe.whitelist()
def import_from_excel_on_submit(docname):
    doc = frappe.get_doc("Project Design Upload", docname)

    # Step 1: Locate attached Excel file
    file_path = None
    for file in frappe.get_all("File", filters={
        "attached_to_doctype": "Project Design Upload",
        "attached_to_name": doc.name
    }, fields=["file_url"]):
        path = get_file_path(file.file_url)
        if path.endswith((".xls", ".xlsx")):
            file_path = path
            break

    if not file_path:
        frappe.throw("No Excel file (.xls or .xlsx) is attached to this Project BOM.")

    # Step 2: Read Excel with header normalization
    df = pd.read_excel(file_path)
    
    # Normalize column names (remove spaces/special chars, make lowercase)
    df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
    df = df.where(pd.notnull(df), None)
    
    # Debug: Show what columns were found
    frappe.logger().debug(f"Excel columns after normalization: {list(df.columns)}")

    # Step 3: Get valid fields
    child_table_fieldname = "items"
    child_doctype = "FG Components"
    valid_fields = [f.fieldname for f in frappe.get_meta(child_doctype).fields]
    
    # Debug: Show field mapping
    frappe.logger().debug(f"Valid fields in {child_doctype}: {valid_fields}")

    # Step 4: Clear existing items
    doc.set(child_table_fieldname, [])

    # Step 5: Append rows with case-insensitive matching
    for _, row in df.iterrows():
        row_data = {}
        for col in row.index:
            # Case-insensitive match to fieldnames
            matched_field = next((f for f in valid_fields if f.lower() == col.lower()), None)
            if matched_field:
                row_data[matched_field] = row[col]
            else:
                frappe.logger().debug(f"Ignoring column '{col}' - no matching field in {child_doctype}")
        
        if row_data:  # Only append if we found matching fields
            # Set default quantity to 1 if not provided in Excel
            if 'quantity' not in row_data or row_data.get('quantity') is None or row_data.get('quantity') == 0:
                row_data['quantity'] = 1
            
            frappe.logger().debug(f"Adding row with data: {row_data}")
            doc.append(child_table_fieldname, row_data)
        else:
            frappe.logger().debug(f"Skipping empty row: {dict(row)}")

    # Step 6: Save
    try:
        doc.save()
        frappe.msgprint(f"Successfully imported {len(df)} rows to {child_table_fieldname}")
        return {"status": "success", "rows": len(df)}
    except Exception as e:
        frappe.log_error(f"Error importing Excel data: {str(e)}")
        frappe.throw("Failed to import data. Please check error logs.")


@frappe.whitelist()
def create_planning_bom(pdu_name):
    """
    Create a new Planning BOM with this Project Design Upload pre-selected
    """
    try:
        # Validate PDU exists
        if not frappe.db.exists("Project Design Upload", pdu_name):
            frappe.throw(f"Project Design Upload {pdu_name} not found")
        
        pdu_doc = frappe.get_doc("Project Design Upload", pdu_name)
        
        # Create new Planning BOM
        planning_bom = frappe.new_doc("Planning BOM")
        planning_bom.project = pdu_doc.project if pdu_doc.project else None
        
        # Add the PDU to the multiselect table
        planning_bom.append("project_design_upload", {
            "project_design": pdu_name
        })
        
        # Save the new Planning BOM
        planning_bom.insert(ignore_permissions=True)
        frappe.db.commit()
        
        return {
            "status": "success",
            "planning_bom_name": planning_bom.name,
            "message": f"Planning BOM {planning_bom.name} created with {pdu_name} pre-selected"
        }
    except Exception as e:
        frappe.log_error(f"Error creating Planning BOM: {str(e)}", "Create Planning BOM Error")
        frappe.throw(f"Failed to create Planning BOM: {str(e)}")