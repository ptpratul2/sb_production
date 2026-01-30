frappe.ui.form.on("Work Order",{
    refresh: function(frm) {
        frm.add_custom_button("Create Off Cut", () => {
        frappe.new_doc("Create Off-Cut Entry", {
            original_rm_code: frm.doc.rm_item_code,
            design_code: frm.doc.fg_item
        });
        });
    }
});