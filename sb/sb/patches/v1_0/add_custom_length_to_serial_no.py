"""
Patch to add custom_length field to Serial No DocType
Run: bench --site [site-name] migrate
"""
import frappe

def execute():
	"""Add custom_length field to Serial No if it doesn't exist"""
	
	# Check if field already exists
	if frappe.db.exists("Custom Field", {"dt": "Serial No", "fieldname": "custom_length"}):
		frappe.log_error("Custom field 'custom_length' already exists on Serial No", "Patch Skipped")
		return
	
	# Create the custom field
	custom_field = frappe.get_doc({
		"doctype": "Custom Field",
		"dt": "Serial No",
		"fieldname": "custom_length",
		"fieldtype": "Float",
		"label": "Length (mm)",
		"insert_after": "warehouse",
		"precision": "2",
		"description": "Length of the serialized item in millimeters"
	})
	
	custom_field.insert(ignore_permissions=True)
	frappe.db.commit()
	
	frappe.log_error("Added custom_length field to Serial No DocType", "Patch Executed")


