frappe.ui.form.on("FG Raw Material Selector", {
    refresh: function (frm) {
        // Prevent adding buttons multiple times
        if (frm._report_buttons_added) return;
        frm._report_buttons_added = true;

        // ────────────────────── FG Reports Dropdown ──────────────────────
        frm.page.add_inner_button(__("Offcut Report"), () => generate_offcut_report(frm), __("FG Reports"));
        frm.page.add_inner_button(__("Cutting Plan Generator"), () => open_cutting_plan_generator(frm), __("FG Reports"));

        // ────────────────────── Create Material Request Button ──────────────────────
        frm.add_custom_button(__("Create Material Request"), function () {
            // Make sure simulation table exists
            if (!frm.doc.rm_oc_simulation || frm.doc.rm_oc_simulation.length === 0) {
                frappe.msgprint({
                    title: __("Missing Data"),
                    message: __("Please run <strong>Calculate RM/OC Usage</strong> first."),
                    indicator: "orange"
                });
                return;
            }

            // Find all NS (Not in Stock) rows → ns_flag = 1
            let ns_rows = frm.doc.rm_oc_simulation.filter(row => cint(row.ns_flag) === 1);

            if (ns_rows.length === 0) {
                frappe.msgprint({
                    title: __("No Shortfall"),
                    message: __("All items are covered by stock. No Material Request needed."),
                    indicator: "blue"
                });
                return;
            }

            // Consolidate by item code
            let items = {};
            ns_rows.forEach(row => {
                let code = row.rm_item_code;
                if (!items[code]) {
                    items[code] = { qty: 0, fg_codes: new Set() };
                }
                items[code].qty += 1;                     // each row = 1 full bar
                if (row.fg_code) items[code].fg_codes.add(row.fg_code);
            });

            // Build nice HTML table
            let rows_html = Object.keys(items).map(code => {
                let i = items[code];
                return `<tr>
                    <td>${code}</td>
                    <td>${i.qty}</td>
                    <td>${Array.from(i.fg_codes).join(", ")}</td>
                </tr>`;
            }).join("");

            let table = `
                <table class="table table-bordered table-sm">
                    <thead class="thead-light">
                        <tr>
                            <th>${__("Item Code")}</th>
                            <th>${__("Required Qty (Nos)")}</th>
                            <th>${__("Linked FG Codes")}</th>
                        </tr>
                    </thead>
                    <tbody>${rows_html}</tbody>
                </table>`;

            // Show confirmation dialog
            let d = new frappe.ui.Dialog({
                title: __("Create Material Request for NS Items"),
                fields: [{
                    fieldtype: "HTML",
                    options: `
                        <p><b>${__("Total NS pieces")}:</b> ${ns_rows.length}</p>
                        <p><b>${__("Distinct raw materials")}:</b> ${Object.keys(items).length}</p>
                        ${table}
                    `
                }],
                primary_action_label: __("Create Material Request"),
                primary_action: function () {
                    d.hide();
                    frappe.call({
                        method: "sb.sb.stock_hooks.create_material_request_for_shortfall",
                        args: { fg_selector_name: frm.doc.name },
                        freeze: true,
                        freeze_message: __("Creating Material Request…"),
                        callback: function (r) {
                            if (r.message && r.message !== "No NS shortfalls to request.") {
                                frappe.show_alert({
                                    message: __("Material Request {0} created", [r.message.link(r.message)]),
                                    indicator: "green"
                                });
                            } else {
                                frappe.msgprint(__("No shortfall items found."));
                            }
                        }
                    });
                }
            });
            d.show();
        }, __("Actions")).addClass("btn-primary");
    }
});

// ────────────────────── Offcut Report ──────────────────────
function generate_offcut_report(frm) {
    frappe.call({
        method: "sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.get_offcut_report",
        args: { fg_selector_name: frm.doc.name },
        callback: function (r) {
            if (!r.message || !r.message.length) {
                frappe.msgprint(__("No offcut data found."));
                return;
            }

            let data = r.message.filter(row => flt(row.remaining_length) >= 200);

            if (!data.length) {
                frappe.msgprint(__("No offcuts ≥ 200 mm found."));
                return;
            }

            new frappe.ui.Dialog({
                title: __("Offcut Report (≥ 200 mm)"),
                size: "large",
                fields: [{ fieldtype: "HTML", options: make_table(data) }],
                primary_action_label: __("Close"),
                secondary_action_label: __("Print"),
                secondary_action: () => print_table(data),
                tertiary_action_label: __("Excel"),
                tertiary_action: () => download_csv(data)
            }).show();
        }
    });
}

function make_table(data) {
    let rows = data.map(r => `
        <tr>
            <td>${r.rm || ""}</td>
            <td>${r.remaining_length || ""}</td>
            <td>${r.quantity || ""}</td>
        </tr>`).join("");

    return `
        <table class="table table-bordered">
            <thead class="thead-light">
                <tr>
                    <th>${__("Raw Material")}</th>
                    <th>${__("Remaining Length (mm)")}</th>
                    <th>${__("Quantity")}</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

function print_table(data) {
    let html = `
        <html><head><title>Offcut Report</title>
        <style>
            body{font-family:Arial;margin:20px;}
            table{border-collapse:collapse;width:100%;}
            th,td{border:1px solid #000;padding:8px;text-align:center;}
            th{background:#f0f0f0;}
        </style>
        </head><body>
        <h2>Offcut Report ≥ 200 mm</h2>
        ${make_table(data)}
        </body></html>`;
    let win = window.open("", "_blank");
    win.document.write(html);
    win.document.close();
    win.print();
}

function download_csv(data) {
    let csv = "Raw Material,Remaining Length (mm),Quantity\n";
    data.forEach(r => csv += `"${r.rm}","${r.remaining_length}","${r.quantity}"\n`);
    let blob = new Blob([csv], { type: "text/csv" });
    let url = URL.createObjectURL(blob);
    let a = document.createElement("a");
    a.href = url;
    a.download = "Offcut_Report_" + frappe.datetime.now_date() + ".csv";
    a.click();
}

// ────────────────────── Cutting Plan Generator ──────────────────────
function open_cutting_plan_generator(frm) {
    frappe.route_options = {
        fg_raw_material_selector: frm.doc.name,
        project: frm.doc.project || ""
    };
    frappe.set_route("query-report", "Cutting Plan Generator");
}