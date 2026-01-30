// Copyright (c) 2025, ptpratul2@gmail.com and contributors
// For license information, please see license.txt

// frappe.ui.form.on("Project Design Upload", {
// 	refresh(frm) {

// 	},
// });


frappe.ui.form.on("Project Design Upload", {
    refresh(frm) {
        // Add status indicator
        if (frm.doc.processed_status) {
            let color = {
                'Open': 'blue',
                'Planned': 'orange',
                'Partially Processed': 'yellow',
                'Processed': 'green'
            }[frm.doc.processed_status] || 'blue';
            
            frm.page.set_indicator(frm.doc.processed_status, color);
        }
        
        if (!frm.is_new()) {
            // Import from Excel button
            frm.add_custom_button("Submit", function () {
                frappe.call({
                    method: "sb.sb.doctype.project_design_upload.project_design_upload.import_from_excel_on_submit",
                    args: { docname: frm.doc.name },
                    callback(r) {
                        if (!r.exc) {
                            frappe.msgprint(`Imported ${r.message.rows} rows from Excel.`);
                            frm.reload_doc();
                        }
                    }
                });
            });
            
            // Create Planning BOM button (Direct button)
            frm.add_custom_button(__('📋 Create Planning BOM'), function() {
                frappe.confirm(
                    __('Create a new Planning BOM with this Project Design Upload pre-selected?'),
                    function() {
                        frappe.call({
                            method: 'sb.sb.doctype.project_design_upload.project_design_upload.create_planning_bom',
                            args: {
                                pdu_name: frm.doc.name
                            },
                            freeze: true,
                            freeze_message: __('Creating Planning BOM...'),
                            callback: function(r) {
                                if (!r.exc && r.message) {
                                    frappe.set_route('Form', 'Planning BOM', r.message.planning_bom_name);
                                    frappe.show_alert({
                                        message: __('Planning BOM created successfully'),
                                        indicator: 'green'
                                    });
                                }
                            }
                        });
                    }
                );
            }).addClass('btn-primary');
        }
    }
});

