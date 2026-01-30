// Copyright (c) 2025, ptpratul2@gmail.com and contributors
// For license information, please see license.txt

frappe.ui.form.on('Create Off-Cut Entry', {
    original_rm_code: function (frm) {
        if (frm.doc.original_rm_code) {
            frappe.call({
                method: "frappe.client.get",
                args: {
                    doctype: "Item",
                    name: frm.doc.original_rm_code
                },
                callback: function (r) {
                    if (r.message) {
                        frm.set_value("original_length", r.message.original_length);
                    }
                }
            });
        }
    },

    validate: function (frm) {
        const codeParts = frm.doc.original_rm_code.split("-");
        const sectionType = codeParts[0];
        const width = codeParts[1];
        const length = frm.doc.balance_length;
        const new_code = `${sectionType}-${width}-${length}-OC`;
        frm.set_value("generated_item_code", new_code);
        frm.set_value("barcode", `${new_code}-${frappe.datetime.now_date().replaceAll("-", "")}-001`);
    },

    submit: function (frm) {
        frappe.call({
            method: "frappe.client.insert",
            args: {
                doc: {
                    doctype: "Item",
                    item_code: frm.doc.generated_item_code,
                    item_group: "Raw Material",
                    is_stock_item: 1,
                    is_off_cut: 1,
                    original_length: frm.doc.original_length,
                    original_rm_code: frm.doc.original_rm_code,
                    design_code: frm.doc.design_code,
                    barcode: frm.doc.barcode,
                }
            },
            callback: function (r) {
                if (r.message) {
                    frappe.msgprint("Off-Cut Item Created!");
                }
            }
        });

        // Add stock via stock entry
        frappe.call({
            method: "erpnext.stock.doctype.stock_entry.stock_entry.make_stock_entry",
            args: {
                item_code: frm.doc.generated_item_code,
                qty: 1,
                purpose: "Material Receipt"
            },
            callback: function (r) {
                if (r.message) {
                    frappe.set_route("Form", "Stock Entry", r.message.name);
                }
            }
        });
    },

    submit_button: function (frm) {
        frappe.call({
            method: "sb.sb.api.create_offcut_item",
            args: {
                data: frm.doc
            },
            callback: function (r) {
                if (r.message === "success") {
                    frappe.msgprint("Off-Cut Item and Stock Entry created successfully.");
                    frm.reload_doc();
                } else {
                    frappe.msgprint("Something went wrong: " + r.message);
                }
            }
        });
    }
});
