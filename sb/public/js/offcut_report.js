/** Format millimetre numbers for display (no decimals for readability). */
function fmt_mm(val) {
    if (val == null || val === "") {
        return "—";
    }
    const n = Math.round(flt(val));
    return n.toLocaleString("en-IN");
}

/**
 * Builds HTML for the NS shortfall preview (before Material Request).
 * Same numbers as the MR (4820 mm bars, cut breakdown by length).
 */
function build_ns_shortfall_report_html(p) {
    const esc = (s) => frappe.utils.escape_html(s == null ? "" : String(s));
    const std_bar = cint(p.std_bar_mm) || 4820;

    const intro = `
        <div class="alert alert-info" style="margin-bottom:16px;border-radius:0px;">
            <p style="margin:0 0 8px 0;font-size:13px;line-height:1.5;">
                ${__(
                    "These are cuts that could not be covered from offcut (OC) and raw material (RM) stock. "
                )}
                <strong>${__("Suggested bar quantity")}</strong>
                ${__(" uses the same rule as the Material Request: total required length ÷ ")}${std_bar}${__(
                    " mm bar length, rounded up."
                )}
            </p>
            <p style="margin:0;font-size:12px;color:var(--text-muted);">
                ${__("FG Raw Material Selector")}: <strong>${esc(p.fg_raw_material_selector || "")}</strong>
            </p>
        </div>`;

    const kpi_box = (label, value) => `
        <div class="col-sm-4" style="margin-bottom:10px;">
            <div style="padding:12px 14px;border-radius:0px;border:1px solid var(--border-color);background:var(--control-bg);">
                <div class="text-muted small">${label}</div>
                <div style="font-size:22px;font-weight:600;">${value}</div>
            </div>
        </div>`;

    const summary = `
        <div class="row" style="margin-bottom:16px;">
            ${kpi_box(__("Uncovered cuts (total)"), cint(p.total_ns_pieces))}
            ${kpi_box(__("Raw materials affected"), cint(p.distinct_rm))}
            ${kpi_box(__("Total length to cover (all items)"), `${fmt_mm(p.grand_total_length_mm)} mm`)}
        </div>`;

    const item_blocks = (p.items || [])
        .map((row) => {
            const name_line = row.item_name
                ? `<div class="small text-muted" style="margin-top:2px;">${esc(row.item_name)}</div>`
                : "";

            const length_table_rows = (row.length_rows || [])
                .map(
                    (lr) =>
                        `<tr>
                            <td class="text-right">${fmt_mm(lr.length_mm)}</td>
                            <td class="text-right">${cint(lr.qty)}</td>
                            <td class="text-right">${fmt_mm(lr.line_mm)}</td>
                        </tr>`
                )
                .join("");

            const length_table =
                (row.length_rows || []).length > 0
                    ? `
                <table class="table table-bordered table-sm" style="margin:10px 0;font-size:13px;">
                    <thead>
                        <tr>
                            <th class="text-right">${__("Cut length (mm)")}</th>
                            <th class="text-right">${__("Cuts (qty)")}</th>
                            <th class="text-right">${__("Length (mm)")}</th>
                        </tr>
                    </thead>
                    <tbody>${length_table_rows}</tbody>
                    <tfoot>
                        <tr>
                            <td colspan="2" class="text-right"><strong>${__("Subtotal")}</strong></td>
                            <td class="text-right"><strong>${fmt_mm(row.total_length_mm)} mm</strong></td>
                        </tr>
                    </tfoot>
                </table>`
                    : `<p class="text-muted small">${__("No length breakdown.")}</p>`;

            const fg_block = row.fg_codes_display
                ? `<div style="margin-top:8px;font-size:12px;">
                    <span class="text-muted">${__("Finished goods affected")}:</span>
                    <span>${esc(row.fg_codes_display)}</span>
                   </div>`
                : "";

            return `
                <div style="margin-bottom:14px;border-radius:0px;border:1px solid var(--border-color);border-left:4px solid var(--primary);background:var(--control-bg);">
                    <div style="padding:14px 16px;">
                        <div class="row">
                            <div class="col-sm-4">
                                <div style="font-size:15px;font-weight:600;">${esc(row.item_code)}</div>
                                ${name_line}
                                <div class="small text-muted" style="margin-top:8px;">
                                    ${__("Uncovered cuts")}: <strong>${cint(row.pieces)}</strong>
                                </div>
                            </div>
                            <div class="col-sm-4">
                                <div class="small text-muted">${__("Total length required")}</div>
                                <div style="font-size:18px;font-weight:600;">${fmt_mm(row.total_length_mm)} mm</div>
                            </div>
                            <div class="col-sm-4">
                                <div class="small text-muted">${__("Suggested purchase qty (bars)")}</div>
                                <div style="font-size:18px;font-weight:600;color:var(--primary);">
                                    ${cint(row.suggested_bar_qty)} ${__("×")} ${std_bar} mm
                                </div>
                            </div>
                        </div>
                        ${length_table}
                        ${fg_block}
                    </div>
                </div>`;
        })
        .join("");

    const scroll_wrap = `
        <div style="max-height:min(60vh,520px);overflow-y:auto;padding-right:4px;">
            ${item_blocks}
        </div>`;

    const footer_note = `
        <p class="text-muted small" style="margin: 0 0 0 0; padding-top: 0px;">
            ${__(
                "Review the figures above, then click Create Material Request to generate a draft purchase request with the same quantities."
            )}
        </p>`;

    return `<div class="sb-ns-shortfall-report">${intro}${summary}${scroll_wrap}${footer_note}</div>`;
}

frappe.ui.form.on("FG Raw Material Selector", {
    refresh: function (frm) {
        // Prevent adding buttons multiple times
        if (frm._report_buttons_added) return;
        frm._report_buttons_added = true;

        // ────────────────────── FG Reports Dropdown ──────────────────────
        frm.page.add_inner_button(__("Offcut Report"), () => generate_offcut_report(frm), __("FG Reports"));
        frm.page.add_inner_button(__("Cutting Plan Generator"), () => open_cutting_plan_generator(frm), __("FG Reports"));

        // ────────────────────── Create Material Request (NIS / NS shortfall) ──────────────────────
        // Uses server-side RM/OC allocation (same as reservation), not removed rm_oc_simulation table.
        frm.add_custom_button(__("Create Material Request"), function () {
            frappe.call({
                method: "sb.sb.stock_hooks.get_ns_mr_shortfall_preview",
                args: { fg_selector_name: frm.doc.name },
                freeze: true,
                freeze_message: __("Computing shortfall…"),
                callback: function (r) {
                    const p = r.message;
                    if (!p || p.empty) {
                        frappe.msgprint({
                            title: __("No Shortfall"),
                            message: __(
                                "All cuts are covered by OC/RM stock, or no valid raw material lines. No Material Request needed."
                            ),
                            indicator: "blue"
                        });
                        return;
                    }

                    const report_html = build_ns_shortfall_report_html(p);

                    const d = new frappe.ui.Dialog({
                        title: __("Shortfall summary — review before Material Request"),
                        size: "extra-large",
                        fields: [
                            {
                                fieldtype: "HTML",
                                options: report_html
                            }
                        ],
                        primary_action_label: __("Create Material Request"),
                        primary_action: function () {
                            d.hide();
                            frappe.call({
                                method: "sb.sb.stock_hooks.create_material_request_for_shortfall",
                                args: { fg_selector_name: frm.doc.name },
                                freeze: true,
                                freeze_message: __("Creating Material Request…"),
                                callback: function (cr) {
                                    if (
                                        cr.message &&
                                        cr.message !== "No NS shortfalls to request."
                                    ) {
                                        const link = frappe.utils.get_form_link(
                                            "Material Request",
                                            cr.message,
                                            true
                                        );
                                        frappe.show_alert({
                                            message: __("Material Request {0} created", [link]),
                                            indicator: "green"
                                        });
                                        frm.reload_doc();
                                    } else {
                                        frappe.msgprint(__("No shortfall items found."));
                                    }
                                }
                            });
                        }
                    });
                    d.show();
                }
            });
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