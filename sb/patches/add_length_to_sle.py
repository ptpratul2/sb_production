# in your custom app, create a patch file like patches/add_length_to_sle.py

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

# def execute():
#     custom_fields = {
#         "Stock Ledger Entry": [
#             dict(fieldname="length", label="Length", fieldtype="Float", insert_after="actual_qty"),
#             dict(fieldname="total_length", label="Total Length", fieldtype="Float", insert_after="length"),
#         ]
#     }
#     create_custom_fields(custom_fields, ignore_validate=True)
