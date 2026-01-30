// Copyright (c) 2025
// For license information, see license.txt

frappe.ui.form.on("Planning BOM", {
    refresh(frm) {
        // Add status indicator
        if (frm.doc.status) {
            let color = {
                'Draft': 'blue',
                'In Progress': 'orange',
                'Partially Processed': 'yellow',
                'Fully Processed': 'green',
                'Cancelled': 'grey'
            }[frm.doc.status] || 'blue';
            
            frm.page.set_indicator(frm.doc.status, color);
        }
        
        // Show progress indicator
        if (frm.doc.items && frm.doc.items.length > 0) {
            let total_required = frm.doc.items.reduce((sum, item) => sum + flt(item.required_qty || item.quantity), 0);
            let total_processed = frm.doc.items.reduce((sum, item) => sum + flt(item.processed_qty), 0);
            
            if (total_required > 0) {
                let percentage = (total_processed / total_required * 100).toFixed(1);
                frm.dashboard.add_progress(
                    __('Processing Progress'),
                    percentage,
                    __('Processed: {0} / {1} ({2}%)', [total_processed.toFixed(2), total_required.toFixed(2), percentage])
                );
            }
        }
        
        if (!frm.is_new()) {
            // ----- Button Group: Actions -----
            frm.add_custom_button("📦 Consolidate Items", () => {
                if (!frm.doc.project_design_upload?.length) {
                    frappe.msgprint("⚠️ Please select at least one Project Design Upload document");
                    return;
                }
                frappe.confirm(
                    __("This will <b>replace</b> all existing items with data from the selected Project Design Upload documents.<br><br>Do you want to continue?"),
                    () => {
                        frappe.call({
                            method: "sb.sb.doctype.planning_bom.planning_bom.consolidate_project_design_uploads",
                            args: { docname: frm.doc.name },
                            freeze: true,
                            freeze_message: __("Processing..."),
                            callback(r) {
                                if (!r.exc && r.message) {
                                    frappe.msgprint({
                                        title: __("✅ Consolidation Complete"),
                                        message: r.message.message,
                                        indicator: "green"
                                    });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                );
            }, __("🔧 Actions")).addClass("btn-primary");

            frm.add_custom_button("👁 Preview Items", () => {
                if (!frm.doc.project_design_upload?.length) {
                    frappe.msgprint("⚠️ Please select at least one Project Design Upload document");
                    return;
                }
                frappe.call({
                    method: "sb.sb.doctype.planning_bom.planning_bom.get_consolidation_preview",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Loading Preview..."),
                    callback(r) {
                        if (!r.exc && r.message) {
                            show_consolidation_preview(r.message);
                        }
                    }
                });
            }, __("🔧 Actions")).addClass("btn-info");

            // ----- Button Group: View -----
            frm.add_custom_button("📄 Show Summary", () => {
                frappe.call({
                    method: "sb.sb.doctype.planning_bom.planning_bom.get_project_design_upload_summary",
                    args: { docname: frm.doc.name },
                    freeze: true,
                    freeze_message: __("Fetching Summary..."),
                    callback(r) {
                        if (!r.exc && r.message) {
                            show_design_upload_summary(r.message);
                        }
                    }
                });
            }, __("📊 View")).addClass("btn-secondary");
            
            // View Pending Items button
            frm.add_custom_button(__('📋 Pending Items'), function() {
                frappe.call({
                    method: 'sb.sb.doctype.planning_bom.planning_bom.get_pending_items',
                    args: {
                        docname: frm.doc.name
                    },
                    callback: function(r) {
                        if (r.message && r.message.length > 0) {
                            show_pending_items_dialog(r.message);
                        } else {
                            frappe.msgprint(__('No pending items found. All items are fully processed.'));
                        }
                    }
                });
            }, __('📊 View')).addClass('btn-info');
            
            // Create FG Raw Material Selector button (Direct button)
            frm.add_custom_button(__('📦 Create FG Raw Material Selector'), function() {
                frappe.confirm(
                    __('Create a new FG Raw Material Selector with this Planning BOM pre-selected?'),
                    function() {
                        frappe.call({
                            method: 'sb.sb.doctype.planning_bom.planning_bom.create_fg_raw_material_selector',
                            args: {
                                planning_bom_name: frm.doc.name
                            },
                            freeze: true,
                            freeze_message: __('Creating FG Raw Material Selector...'),
                            callback: function(r) {
                                if (!r.exc && r.message) {
                                    frappe.set_route('Form', 'FG Raw Material Selector', r.message.fg_selector_name);
                                    frappe.show_alert({
                                        message: __('FG Raw Material Selector created successfully'),
                                        indicator: 'green'
                                    });
                                }
                            }
                        });
                    }
                );
            }).addClass('btn-primary');
        }
    },

    project_design_upload_on_form_rendered(frm) {
        if (frm.doc.project_design_upload?.length) {
            update_summary_section(frm);
            auto_fetch_project(frm);
        }
    }
});

// Auto-fetch project from selected Project Design Uploads
function auto_fetch_project(frm) {
    if (!frm.doc.project_design_upload || frm.doc.project_design_upload.length === 0) {
        frm.set_value('project', '');
        return;
    }
    
    // Get the first selected PDU's project
    let first_pdu = frm.doc.project_design_upload[0].project_design;
    
    if (first_pdu) {
        frappe.call({
            method: 'frappe.client.get_value',
            args: {
                doctype: 'Project Design Upload',
                filters: {name: first_pdu},
                fieldname: 'project'
            },
            callback: function(r) {
                if (r.message && r.message.project) {
                    frm.set_value('project', r.message.project);
                }
            }
        });
    }
}

// ------------------ UI Helper Functions ------------------

// Consolidation Preview
function show_consolidation_preview(data) {
    let preview_html = `
        <div class="consolidation-preview">
            <h4 class="mb-3">📦 Consolidation Preview</h4>
            <p>
                <span class="badge badge-primary">Total Items: ${data.preview.length}</span>
                <span class="badge badge-success">Total Quantity: ${data.total_quantity}</span>
                <span class="badge badge-info">Total Unit Area: ${data.total_unit_area}</span>
            </p>
            <table class="table table-sm table-hover">
                <thead class="thead-dark">
                    <tr>
                        <th>FG Code</th>
                        <th>Item Code</th>
                        <th>Dimension</th>
                        <th class="text-right">Quantity</th>
                        <th>Sources</th>
                    </tr>
                </thead>
                <tbody>
    `;
    data.preview.forEach(item => {
        preview_html += `
            <tr>
                <td>${frappe.utils.escape_html(item.fg_code || '')}</td>
                <td>${frappe.utils.escape_html(item.item_code || '')}</td>
                <td>${frappe.utils.escape_html(item.dimension || '')}</td>
                <td class="text-right"><b>${item.quantity}</b></td>
                <td><span class="badge badge-info">${item.source_count} doc(s)</span></td>
            </tr>
        `;
    });
    preview_html += `</tbody></table></div>`;

    frappe.msgprint({
        title: "📝 Consolidation Preview",
        message: preview_html,
        wide: true
    });
}

// Summary of PDUs
function show_design_upload_summary(data) {
    let summary_html = `
        <div class="design-upload-summary">
            <h4 class="mb-3">📄 Selected Project Design Upload Summary</h4>
            <p>
                <span class="badge badge-primary">Documents: ${data.total_documents}</span>
                <span class="badge badge-success">Total Items: ${data.total_items}</span>
            </p>
            <table class="table table-sm table-hover">
                <thead class="thead-dark">
                    <tr>
                        <th>Document Name</th>
                        <th>Project</th>
                        <th>Upload Date</th>
                        <th class="text-right">Item Count</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>
    `;
    data.summary.forEach(doc => {
        summary_html += `
            <tr>
                <td><a href="/app/project-design-upload/${doc.name}" target="_blank">${doc.name}</a></td>
                <td>${doc.project || ''}</td>
                <td>${doc.upload_date || ''}</td>
                <td class="text-right"><b>${doc.item_count}</b></td>
                <td><span class="indicator ${doc.processed_status === 'Completed' ? 'green' : 'orange'}">
                    ${doc.processed_status || 'Pending'}
                </span></td>
            </tr>
        `;
    });
    summary_html += `</tbody></table></div>`;

    frappe.msgprint({
        title: "📊 Project Design Upload Summary",
        message: summary_html,
        wide: true
    });
}

// Update Summary Dashboard Indicators
function update_summary_section(frm) {
    frappe.call({
        method: "sb.sb.doctype.planning_bom.planning_bom.get_project_design_upload_summary",
        args: { docname: frm.doc.name },
        callback(r) {
            if (!r.exc && r.message) {
                frm.dashboard.clear_headline();
                frm.dashboard.add_indicator(
                    `📄 Selected Documents: ${r.message.total_documents}`,
                    "blue"
                );
                frm.dashboard.add_indicator(
                    `📦 Total Items: ${r.message.total_items}`,
                    "green"
                );
                frm.dashboard.show();
            }
        }
    });
}

frappe.ui.form.on("Project Design Multiselect", {
    project_design_upload_add(frm) {
        update_summary_section(frm);
        auto_fetch_project(frm);
    },
    project_design_upload_remove(frm) {
        update_summary_section(frm);
        auto_fetch_project(frm);
    }
});

// Show Pending Items Dialog
function show_pending_items_dialog(pending_items) {
    let html = `
        <table class="table table-bordered table-sm">
            <thead>
                <tr>
                    <th>FG Code</th>
                    <th>Item Code</th>
                    <th class="text-right">Required</th>
                    <th class="text-right">Processed</th>
                    <th class="text-right">Remaining</th>
                    <th>Project</th>
                </tr>
            </thead>
            <tbody>
    `;
    
    let total_remaining = 0;
    pending_items.forEach(item => {
        total_remaining += item.remaining_qty;
        html += `
            <tr>
                <td>${item.fg_code || ''}</td>
                <td>${item.item_code || ''}</td>
                <td class="text-right">${item.required_qty.toFixed(2)}</td>
                <td class="text-right">${item.processed_qty.toFixed(2)}</td>
                <td class="text-right"><b>${item.remaining_qty.toFixed(2)}</b></td>
                <td>${item.project || ''}</td>
            </tr>
        `;
    });
    
    html += `
            </tbody>
            <tfoot>
                <tr class="font-weight-bold">
                    <td colspan="4" class="text-right">Total Remaining:</td>
                    <td class="text-right"><b>${total_remaining.toFixed(2)}</b></td>
                    <td></td>
                </tr>
            </tfoot>
        </table>
    `;
    
    frappe.msgprint({
        title: __('Pending Items ({0})', [pending_items.length]),
        message: html,
        wide: true
    });
}
