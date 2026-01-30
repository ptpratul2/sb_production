frappe.query_reports["Offcut Report"] = {
    "filters": [
        {
            "fieldname": "fg_selector_name",
            "label": __("FG Raw Material Selector"),
            "fieldtype": "Link",
            "options": "FG Raw Material Selector",
            "reqd": 1
        }
    ],
    "onload": function(report) {
        report.page.add_inner_button(__("Show Chart"), function () {
            let data = frappe.query_report.data; // ✅ Correct way in v15

            if (!data || !data.length) {
                frappe.msgprint(__("No data available to display the chart."));
                return;
            }

            // Prepare chart data
            let chart_data = {
                labels: data.map(row => row.rm),
                datasets: [
                    {
                        name: __("Quantity"),
                        values: data.map(row => row.quantity)
                    },
                    {
                        name: __("Remaining Length"),
                        values: data.map(row => row.remaining_length)
                    }
                ]
            };

            // Remove any old chart
            if (report.$chart_area) {
                report.$chart_area.remove();
            }

            // Create a chart container at the top (below filters)
            report.$chart_area = $('<div class="chart-area" style="margin-bottom: 20px;"></div>')
                .prependTo(report.page.body);

            // Render the chart inside this container
            new frappe.Chart(report.$chart_area[0], {
                title: __("Offcut Chart"),
                data: chart_data,
                type: 'bar',
                height: 300,
                colors: ['#21ba45', '#2185d0']
            });
        });

        report.page.add_inner_button(__("Create Offcut Stock Entries"), function() {
            let fg_selector_name = frappe.query_report.get_filter_value("fg_selector_name");
            if (!fg_selector_name) {
                frappe.msgprint(__("Please select an FG Raw Material Selector."));
                return;
            }

            frappe.call({
                method: "sb.sb.report.offcut_report.offcut_report.create_offcut_stock_entries_from_report",
                args: {
                    fg_selector_name: fg_selector_name
                },
                callback: function(r) {
                    frappe.msgprint(r.message);
                    frappe.query_report.refresh();
                }
            });
        });
    }
};
