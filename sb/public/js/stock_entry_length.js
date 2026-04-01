// Stock Entry Detail: auto-fill custom_total_length from custom_length × stock qty.
// Uses transfer_qty when set (matches Stock Ledger / server), else qty.

function stock_qty_for_length(row) {
	const tq = flt(row.transfer_qty);
	if (tq) {
		return tq;
	}
	return flt(row.qty);
}

async function recalc_row_length_and_weight(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row) {
		return;
	}

	const len = flt(row.custom_length);
	const qty = stock_qty_for_length(row);

	if (len && qty) {
		frappe.model.set_value(cdt, cdn, "custom_total_length", len * qty);
	} else if (!len) {
		frappe.model.set_value(cdt, cdn, "custom_total_length", 0);
	}

	if (!row.item_code || !len || !qty) {
		return;
	}

	try {
		const r = await frappe.db.get_value(
			"Item Weight Conversion",
			{ item: row.item_code, length: row.custom_length },
			"weightpiece"
		);

		if (r && r.message && r.message.weightpiece != null) {
			const weight_per_unit = flt(r.message.weightpiece);
			frappe.model.set_value(cdt, cdn, "custom_weight_per_unit", weight_per_unit);
			frappe.model.set_value(cdt, cdn, "custom_total_weight", weight_per_unit * qty);
		} else {
			frappe.model.set_value(cdt, cdn, "custom_weight_per_unit", 0);
			frappe.model.set_value(cdt, cdn, "custom_total_weight", 0);
		}
	} catch (err) {
		frappe.log_error(err);
	}
}

frappe.ui.form.on("Stock Entry Detail", {
	custom_length(frm, cdt, cdn) {
		recalc_row_length_and_weight(frm, cdt, cdn);
	},
	qty(frm, cdt, cdn) {
		recalc_row_length_and_weight(frm, cdt, cdn);
	},
	transfer_qty(frm, cdt, cdn) {
		recalc_row_length_and_weight(frm, cdt, cdn);
	},
	conversion_factor(frm, cdt, cdn) {
		recalc_row_length_and_weight(frm, cdt, cdn);
	},
	item_code(frm, cdt, cdn) {
		recalc_row_length_and_weight(frm, cdt, cdn);
	},
});
