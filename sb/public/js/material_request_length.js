frappe.ui.form.on('Material Request Item', {
    custom_length: function(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (row.custom_length && row.qty) {
            row.custom_total_length = row.custom_length * row.qty;
            frm.refresh_field("items");
        }
    },
    qty: function(frm, cdt, cdn) {
        let row = locals[cdt][cdn];
        if (row.custom_length && row.qty) {
            row.custom_total_length = row.custom_length * row.qty;
            frm.refresh_field("items");
        }
    }
});
