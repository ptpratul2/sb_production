// Copyright (c) 2025, ptpratul2@gmail.com and contributors
// For license information, please see license.txt


frappe.query_reports["Raw Material Cutting Report"] = {
    "filters": [
        {
            "fieldname": "fg_raw_material_selector",
            "label": __("FG Raw Material Selector"),
            "fieldtype": "Link",
            "options": "FG Raw Material Selector",
            "reqd": 1
        },
        {
            "fieldname": "project",
            "label": __("Project"),
            "fieldtype": "Link",
            "options": "Project",
            "reqd": 0
        },
        {
            "fieldname": "ipo_name",
            "label": __("IPO Name"),
            "fieldtype": "Data",
            "reqd": 0
        },
        {
            "fieldname": "code",
            "label": __("FG Code"),
            "fieldtype": "Data",
            "reqd": 0
        },
        {
            "fieldname": "section",
            "label": __("Section"),
            "fieldtype": "Select",
            "options": "\nCHANNEL SECTION\nL SECTION\nIC SECTION\nJ SECTION\nT SECTION\nSOLDIER\nEXTERNAL CORNER\nRK",
            "reqd": 0
        }
    ]
}