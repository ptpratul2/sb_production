frappe.ui.form.on('FG Raw Material Selector', {
    refresh: function (frm) {
        // Set default company if not set
        if (!frm.doc.company && frm.is_new()) {
            frappe.db.get_value('User', frappe.session.user, 'company', (r) => {
                if (r && r.company) {
                    frm.set_value('company', r.company);
                }
            });
        }

        // Add status indicator
        if (frm.doc.status) {
            let color = {
                'Draft': 'blue',
                'In Progress': 'orange',
                'Submitted': 'purple',
                'Completed': 'green',
                'Cancelled': 'grey'
            }[frm.doc.status] || 'blue';

            frm.page.set_indicator(frm.doc.status, color);
        }

        if (!frm.is_new() && frm.doc.docstatus === 0) {
            // ----- Button: Fetch Pending Items (NEW APPROACH) -----
            frm.add_custom_button(__('Fetch Pending Items'), function () {
                if (!frm.doc.planning_bom || frm.doc.planning_bom.length === 0) {
                    frappe.msgprint(__('Please select at least one Planning BOM.'));
                    return;
                }

                frappe.confirm(
                    __('This will fetch all pending items (remaining_qty > 0) from selected Planning BOMs. Continue?'),
                    function () {
                        frappe.call({
                            method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.fetch_pending_items',
                            args: {
                                docname: frm.doc.name
                            },
                            freeze: true,
                            freeze_message: __('Fetching pending items...'),
                            callback: function (r) {
                                if (!r.exc && r.message) {
                                    frappe.show_alert({
                                        message: r.message.message,
                                        indicator: 'green'
                                    });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                );
            }, __('Actions')).addClass('btn-primary');

            // ----- Button: Process FG Codes (OLD APPROACH - Keep for compatibility) -----
            frm.add_custom_button(__('Process FG Codes'), function () {
                if (!frm.doc.planning_bom || frm.doc.planning_bom.length === 0) {
                    frappe.msgprint(__('Please select at least one Planning BOM.'));
                    return;
                }

                const pdu_list = frm.doc.planning_bom.map(row => row.planning_bom);
                frappe.show_alert({ message: __('Queuing raw material processing...'), indicator: 'blue' });

                frappe.call({
                    method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.get_raw_materials',
                    args: {
                        docname: frm.doc.name,
                        planning_bom: pdu_list
                    },
                    callback: function (r) {
                        if (!r.exc) {
                            frappe.show_alert({ message: __('Processing has been queued.'), indicator: 'green' });
                        }
                    }
                });
            }, __('Actions')).addClass('btn-secondary');

        }
        // ----- Button: Calculate RM/OC Usage -----
        if (!frm.is_new() && frm.doc.docstatus === 0 && frm.doc.status === "In Progress") {
            frm.add_custom_button(__('Calculate RM/OC Usage'), function () {
                frappe.call({
                    method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.rm_oc_calculator',
                    args: {
                        fg_selector_name: frm.doc.name
                    },
                    freeze: true,
                    freeze_message: __('Calculating RM/OC usage...'),
                    callback: function (r) {
                        if (!r.exc && r.message) {
                            frappe.show_alert({
                                message: r.message,
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        }
                    }
                });
            }, __('Actions')).addClass('btn-primary');
        }
        // ===== ADD THIS INSIDE refresh: function(frm) { ... } =====

        // Button: Reserve Stock Physically (only after RM/OC calculation)
        if (
            !frm.is_new() &&
            frm.doc.docstatus === 0 &&
            ["In Progress", "Submitted"].includes(frm.doc.status) &&
            frm.doc.rm_oc_simulation &&
            frm.doc.rm_oc_simulation.length > 0
        ) {
            // Disable button if a reservation already exists
            let already_reserved = frm.doc.raw_materials.some(row => row.reserve_tag === 1 && row.stock_entry);

            frm.add_custom_button(
                __('Reserve Stock Physically'),
                function () {
                    if (already_reserved) {
                        frappe.msgprint(__('Stock is already reserved via Stock Entry {0}', [frm.doc.raw_materials.find(r => r.stock_entry)?.stock_entry || '']));
                        return;
                    }

                    frappe.confirm(
                        __('This will create a Material Transfer Stock Entry and move all allocated OC/RM pieces to <b>{0}</b>.<br><br>Continue?', [frm.doc.reserved_warehouse || 'Reserved Stock - DD']),
                        function () {
                            frappe.call({
                                method: 'sb.sb.stock_reserve.reserve_stock_physically',
                                args: {
                                    fg_selector_name: frm.doc.name
                                },
                                // freeze: true,  <-- REMOVED freeze to allow background processing
                                // freeze_message: __('Reserving stock physically...'),
                                callback: function (r) {
                                    if (r.message && r.message.status === "queued") {
                                        frappe.show_alert({
                                            message: r.message.message,
                                            indicator: 'blue'
                                        });
                                    } else if (r.message && r.message.status === "success") {
                                        // Fallback for immediate success (if logic changes back)
                                        frappe.show_alert({
                                            message: __('Stock reserved successfully!<br>Stock Entry: {0}', [r.message.stock_entry]),
                                            indicator: 'green'
                                        });
                                        frm.reload_doc();
                                    } else {
                                        frappe.msgprint(r.message?.message || __('Reservation failed'));
                                    }
                                }
                            });
                        }
                    );
                },
                __('Actions')
            )
                .css({ 'background-color': '#2490ef', 'color': 'white', 'font-weight': 'bold' })
                .prop('disabled', already_reserved);

            if (already_reserved) {
                frm.get_field("raw_materials").grid.cannot_add_rows = true;
            }
        }

        // Listen for stock reservation completion
        frappe.realtime.on('stock_reservation_done', function (data) {
            if (frm.doc.name === data.message.docname) {
                frappe.show_alert({
                    message: data.message.message,
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        });
        // Status buttons for submitted documents
        if (frm.doc.docstatus === 1 && frm.doc.status === "Submitted") {
            frm.add_custom_button(__('Mark as Completed'), function () {
                frappe.confirm(
                    __('Mark this selector as completed?'),
                    function () {
                        frappe.call({
                            method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.mark_as_completed',
                            args: {
                                docname: frm.doc.name
                            },
                            callback: function (r) {
                                if (!r.exc) {
                                    frappe.show_alert({
                                        message: __('Status updated to Completed'),
                                        indicator: 'green'
                                    });
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                );
            }, __('Status')).addClass('btn-success');
        }

        // OLD Status Management Buttons (kept for backward compatibility)
        if (frm.doc.docstatus === 0) {
            // Mark as Completed button (only show if status is "In Progress")
            if (frm.doc.status === "In Progress") {
                frm.add_custom_button(__('Mark as Completed'), function () {
                    frappe.confirm(
                        __('Are you sure you want to mark this FG Raw Material Selector as Completed?'),
                        function () {
                            frappe.call({
                                method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.mark_as_completed',
                                args: {
                                    docname: frm.doc.name
                                },
                                freeze: true,
                                freeze_message: __('Updating status...'),
                                callback: function (r) {
                                    if (!r.exc) {
                                        frappe.show_alert({
                                            message: __('Status updated to Completed'),
                                            indicator: 'green'
                                        });
                                        frm.reload_doc();
                                    }
                                }
                            });
                        }
                    );
                }, __('Status')).addClass('btn-success');
            }

            // Cancel button (show for all statuses except Cancelled)
            if (frm.doc.status !== "Cancelled") {
                frm.add_custom_button(__('Cancel'), function () {
                    frappe.confirm(
                        __('Are you sure you want to cancel this FG Raw Material Selector?'),
                        function () {
                            frappe.call({
                                method: 'sb.sb.doctype.fg_raw_material_selector.fg_raw_material_selector.cancel_document',
                                args: {
                                    docname: frm.doc.name
                                },
                                freeze: true,
                                freeze_message: __('Cancelling...'),
                                callback: function (r) {
                                    if (!r.exc) {
                                        frappe.show_alert({
                                            message: __('Document cancelled'),
                                            indicator: 'red'
                                        });
                                        frm.reload_doc();
                                    }
                                }
                            });
                        }
                    );
                }, __('Status')).addClass('btn-danger');
            }
        }

        // Listen for real-time job completion
        frappe.realtime.on('fg_materials_done', function (data) {
            if (frm.doc.name === data.docname) {
                frappe.show_alert({ message: __('FG Raw Material processing is complete.'), indicator: 'green' });
                frm.reload_doc();
            }
        });
    },

    planning_bom_on_form_rendered(frm) {
        if (frm.doc.planning_bom?.length) {
            auto_fetch_project_from_planning_bom(frm);
        }
    }
});

// Auto-fetch project from selected Planning BOMs
function auto_fetch_project_from_planning_bom(frm) {
    if (!frm.doc.planning_bom || frm.doc.planning_bom.length === 0) {
        frm.set_value('project', '');
        return;
    }

    // Get the first selected Planning BOM's project
    let first_pbom = frm.doc.planning_bom[0].planning_bom;

    if (first_pbom) {
        frappe.call({
            method: 'frappe.client.get_value',
            args: {
                doctype: 'Planning BOM',
                filters: { name: first_pbom },
                fieldname: 'project'
            },
            callback: function (r) {
                if (r.message && r.message.project) {
                    frm.set_value('project', r.message.project);
                }
            }
        });
    }
}

// Child table events for Planning BOM Multiselect
frappe.ui.form.on("Planning BOM Multiselect", {
    planning_bom_add(frm) {
        auto_fetch_project_from_planning_bom(frm);
    },
    planning_bom_remove(frm) {
        auto_fetch_project_from_planning_bom(frm);
    }
});

// Auto-fill warehouse in raw_materials table when warehouse fields change
frappe.ui.form.on('FG Raw Material Selector', {
    raw_material_warehouse: function (frm) {
        update_raw_materials_warehouse(frm);
    },
    offcut_warehouse: function (frm) {
        update_raw_materials_warehouse(frm);
    },
    raw_materials_add: function (frm, cdt, cdn) {
        update_single_row_warehouse(frm, cdt, cdn);
    },
    raw_materials_refresh: function (frm) {
        update_raw_materials_warehouse(frm);
    }
});

// Function to update warehouse for all raw_materials rows
function update_raw_materials_warehouse(frm) {
    if (!frm.doc.raw_material_warehouse && !frm.doc.offcut_warehouse) {
        return;
    }

    // Determine which warehouse to use based on status
    // For IS items, use raw_material_warehouse, for NIS items, could use either
    // Default to raw_material_warehouse if available
    let default_warehouse = frm.doc.raw_material_warehouse || frm.doc.offcut_warehouse;

    if (default_warehouse && frm.doc.raw_materials) {
        frm.doc.raw_materials.forEach(function (row) {
            // Only update if warehouse is empty or if status is IS/NIS
            if (!row.warehouse || row.warehouse === '') {
                // For IS items, prefer raw_material_warehouse
                // For NIS items, could use either, but default to raw_material_warehouse
                if (row.status === 'IS' && frm.doc.raw_material_warehouse) {
                    frappe.model.set_value(row.doctype, row.name, 'warehouse', frm.doc.raw_material_warehouse);
                } else if (default_warehouse) {
                    frappe.model.set_value(row.doctype, row.name, 'warehouse', default_warehouse);
                }
            }
        });
        frm.refresh_field('raw_materials');
    }
}

// Function to update warehouse for a single row when added
function update_single_row_warehouse(frm, cdt, cdn) {
    let row = frappe.get_doc(cdt, cdn);
    if (!row.warehouse || row.warehouse === '') {
        // Determine warehouse based on status
        if (row.status === 'IS' && frm.doc.raw_material_warehouse) {
            frappe.model.set_value(cdt, cdn, 'warehouse', frm.doc.raw_material_warehouse);
        } else if (frm.doc.raw_material_warehouse) {
            frappe.model.set_value(cdt, cdn, 'warehouse', frm.doc.raw_material_warehouse);
        } else if (frm.doc.offcut_warehouse) {
            frappe.model.set_value(cdt, cdn, 'warehouse', frm.doc.offcut_warehouse);
        }
    }
}
