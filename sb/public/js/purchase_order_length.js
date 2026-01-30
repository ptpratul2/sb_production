frappe.ui.form.on('Purchase Order Item', {
    custom_length: async function(frm, cdt, cdn) {
        let row = locals[cdt][cdn];

        if (row.custom_length && row.qty) {
            // Update custom total length (can be set directly if it's editable)
            frappe.model.set_value(cdt, cdn, "custom_total_length", row.custom_length * row.qty);

            try {
                let r = await frappe.db.get_value(
                    "Item Weight Conversion",
                    { "item": row.item_code, "length": row.custom_length },
                    "weightpiece"
                );

                if (r && r.message) {
                    let weight_per_unit = r.message.weightpiece || 0;
                    let total_weight = weight_per_unit * row.qty;

                    frappe.model.set_value(cdt, cdn, "weight_per_unit", weight_per_unit);
                    frappe.model.set_value(cdt, cdn, "total_weight", total_weight);
                    frappe.model.set_value(cdt, cdn, "weight_uom", "Kg");

                    console.log("Weight per unit:", weight_per_unit);
                } else {
                    frappe.model.set_value(cdt, cdn, "weight_per_unit", 0);
                    frappe.model.set_value(cdt, cdn, "total_weight", 0);
                    
                    console.log("No matching Item Weight Conversion found");
                }

            } catch (err) {
                console.error("Error fetching weight:", err);
            }
        }
    },

    qty: async function(frm, cdt, cdn) {
        let row = locals[cdt][cdn];

        if (row.custom_length && row.qty) {
            frappe.model.set_value(cdt, cdn, "custom_total_length", row.custom_length * row.qty);

            try {
                let r = await frappe.db.get_value(
                    "Item Weight Conversion",
                    { "item": row.item_code, "length": row.custom_length },
                    "weightpiece"
                );

                if (r && r.message) {
                    let weight_per_unit = r.message.weightpiece || 0;
                    let total_weight = weight_per_unit * row.qty;

                    frappe.model.set_value(cdt, cdn, "weight_per_unit", weight_per_unit);
                    frappe.model.set_value(cdt, cdn, "total_weight", total_weight);

                    console.log("Weight per unit:", weight_per_unit);
                }

            } catch (err) {
                console.error("Error fetching weight:", err);
            }
        }
    }
});
