import frappe
from frappe.utils import flt
# No modification needed here; the import is correct.
# If you see "flt not imported" error, ensure frappe is installed and your environment is correct.
import re
import json

def parse_dimension(dimension):
    if not dimension: 
        return "", ""
    dimension = str(dimension).strip("()")
    parts = dimension.split(",")
    l1 = parts[0].strip() if parts else ""
    l2 = parts[1].strip() if len(parts) > 1 else "-"
    frappe.log_error(
        message=f"Parsing dimension: {dimension} -> L1={l1}, L2={l2}",
        title="Dimension Parse Debug"
    )
    return l1, l2

def normalize_fieldname(name):
    name = name.lower()
    name = re.sub(r"[^a-z0-9]", "_", name)
    return name

def execute(filters=None):
    try:
        if not filters or not filters.get("fg_raw_material_selector"):
            frappe.throw("Please select FG Raw Material Selector")

        try:
            doc = frappe.get_doc("FG Raw Material Selector", filters["fg_raw_material_selector"])
        except frappe.DoesNotExistError:
            frappe.throw(f"FG Raw Material Selector '{filters['fg_raw_material_selector']}' does not exist")

        if not hasattr(doc, 'raw_materials') or not doc.raw_materials:
            frappe.msgprint("No raw material data found.")
            return [], []

        consolidated = {}

        raw_material_sections = ["CH SECTION", "CH SECTION CORNER", "L SECTION", "IC SECTION", "J SECTION", "T SECTION", "SOLDIER", "MISC SECTION"]
        predefined_child_parts = {
            "B SIDE RAIL": "b_side_rail",
            "SIDE RAIL": "b_side_rail",
            "STIFFNER PLATE": "stiffner_plate",
            "STIFF PLATE": "stiffner_plate",
            "STIFFENER PLATE": "stiffner_plate",
            "PLATE STIFFNER": "stiffner_plate",
            "ROUND PIPE": "round_pipe",
            "SQUARE PIPE": "square_pipe",
            "ROCKER": "rocker",
            "RK-50": "rocker",
            "U STIFFNER": "stiffner_u",
            "H STIFFNER": "stiffner_h",
            "I STIFFNER": "stiffner_i",
            "OUTER CAP": "outer_cap"
        }

        for item in doc.raw_materials:
            remark = (item.get("remark") or "").upper()
            item_code = (item.get("item_code") or "").upper()
            fg_code = (item.get("fg_code") or "").upper()
            item_ipo_name = (item.get("ipo_name") or "").upper()
            item_project = item.get("project", "")
            planning_bom_item_reference = item.get("planning_bom_item_reference") or ""
            l1, l2 = parse_dimension(item.get("dimension"))
            quantity = int(flt(item.get("quantity", 0)))
            a = item.get("a", 0)
            b = item.get("b", 0)
            sec_code = item.get("code", "")
            l1, l2 = parse_dimension(item.get("dimension"))
            bom_l1 = item.get("l1", 0)
            bom_l2 = item.get("l2", 0)
            bom_qty = item.get("bom_qty", 0)
            dwg_no = item.get("dwn_no", "")
            u_area = item.get("u_area", "")
            fg_code = fg_code or item.get("fg_code", "")

            consolidation_key = planning_bom_item_reference or f"{fg_code}_{item_ipo_name}_{item_project}_{item_code}"

            if consolidation_key not in consolidated:
                consolidated[consolidation_key] = {
                    "fg_code": fg_code,
                    "ipo_name": item_ipo_name,
                    "project": item_project,
                    "section": remark,
                    "a": a,
                    "b": b,
                    "code": sec_code,
                    "l1": bom_l1,
                    "l2": bom_l2,
                    "dwg_no": dwg_no,
                    "fg_code": fg_code,
                    "total_quantity": bom_qty,
                    "u_area": u_area,   
                    "total_area": u_area * bom_qty if u_area else 0,
                    "rm1_code": "", "rm1_l1": "", "rm1_l2": "", "rm1_qty": 0,
                    "rm2_code": "", "rm2_l1": "", "rm2_l2": "", "rm2_qty": 0,
                    "rm3_code": "", "rm3_l1": "", "rm3_l2": "", "rm3_qty": 0,
                    "b_side_rail_l1": "", "b_side_rail_l2": "", "b_side_rail_qty": 0,
                    "outer_cap_l1": "", "outer_cap_l2": "", "outer_cap_qty": 0,
                    "rocker_l1": "", "rocker_l2": "", "rocker_qty": 0,
                    "round_pipe_l1": "", "round_pipe_l2": "", "round_pipe_qty": 0,
                    "square_pipe_l1": "", "square_pipe_l2": "", "square_pipe_qty": 0,
                    "stiffner_h_l1": "", "stiffner_h_l2": "", "stiffner_h_qty": 0,
                    "stiffner_i_l1": "", "stiffner_i_l2": "", "stiffner_i_qty": 0,
                    "stiffner_plate_l1": "", "stiffner_plate_l2": "", "stiffner_plate_qty": 0,
                    "stiffner_u_l1": "", "stiffner_u_l2": "", "stiffner_u_qty": 0,
                }

            is_raw_material = (
                (remark in raw_material_sections or 
                 item_code == "130 L" or
                 any(section in remark for section in ["SECTION", "ANGLE", "CHANNEL", "BEAM"]) or
                 any(section in item_code for section in ["L", "C", "I", "H", "T"]))
            )

            if is_raw_material and remark != "CHILD PART":
                rm_assigned = False
                for rm_slot in ["rm1", "rm2", "rm3"]:
                    if quantity > 0 and not consolidated[consolidation_key][f"{rm_slot}_code"]:
                        consolidated[consolidation_key][f"{rm_slot}_code"] = item_code
                        consolidated[consolidation_key][f"{rm_slot}_l1"] = l1
                        consolidated[consolidation_key][f"{rm_slot}_l2"] = l2
                        consolidated[consolidation_key][f"{rm_slot}_qty"] = quantity
                        rm_assigned = True
                    if rm_assigned:
                        break


            part_fieldname = None
            if item_code in predefined_child_parts:
                part_fieldname = predefined_child_parts[item_code]

            ## Check if item section matches not with "Misc Section"
            if part_fieldname == "rocker" and remark == "MISC SECTION":
                part_fieldname = None
            elif remark == "CHILD PART":
                for part_name, field_name in predefined_child_parts.items():
                    if part_name in item_code or item_code in part_name:
                        part_fieldname = field_name
                        break

            if part_fieldname and f"{part_fieldname}_l1" in consolidated[consolidation_key]:
                if not consolidated[consolidation_key].get(f"{part_fieldname}_l1"):
                    consolidated[consolidation_key][f"{part_fieldname}_l1"] = l1
                    consolidated[consolidation_key][f"{part_fieldname}_l2"] = l2
                consolidated[consolidation_key][f"{part_fieldname}_qty"] += flt(item.get("quantity", 0))


        # Return as data table
        data = []
        for key, row in consolidated.items():
            row_data = row.copy()
            row_data["consolidation_key"] = key
            data.append(row_data)

        columns = [
            # {"label": "Sr No", "fieldname": "sr_no", "fieldtype": "Int"},
            {"label": "FG Code", "fieldname": "fg_code", "fieldtype": "Data"},
            {"label": "Project", "fieldname": "project", "fieldtype": "Data"},
            {"label": "IPO Name", "fieldname": "ipo_name", "fieldtype": "Data"},
            {"label": "Section", "fieldname": "section", "fieldtype": "Data"},
            {"label": "A", "fieldname": "a", "fieldtype": "Data"},
            {"label": "B", "fieldname": "b", "fieldtype": "Data"},
            {"label": "Code", "fieldname": "code", "fieldtype": "Data"},         
            {"label": "L1", "fieldname": "l1", "fieldtype": "Data"},
            {"label": "L2", "fieldname": "l2", "fieldtype": "Data"},
            {"label": "Drawing No", "fieldname": "dwg_no", "fieldtype": "Data"},
            {"label": "Total Qty", "fieldname": "total_quantity", "fieldtype": "Float"},
            {"label": "U Area", "fieldname": "u_area", "fieldtype": "Data"},
            {"label": "Total Area", "fieldname": "total_area", "fieldtype": "Float"},
            {"label": "RM1 Code", "fieldname": "rm1_code", "fieldtype": "Data"},
            {"label": "RM1 L1", "fieldname": "rm1_l1", "fieldtype": "Data"},
            {"label": "RM1 L2", "fieldname": "rm1_l2", "fieldtype": "Data"},
            {"label": "RM1 Qty", "fieldname": "rm1_qty", "fieldtype": "Float"},
            {"label": "RM2 Code", "fieldname": "rm2_code", "fieldtype": "Data"},
            {"label": "RM2 L1", "fieldname": "rm2_l1", "fieldtype": "Data"},
            {"label": "RM2 L2", "fieldname": "rm2_l2", "fieldtype": "Data"},
            {"label": "RM2 Qty", "fieldname": "rm2_qty", "fieldtype": "Float"},
            {"label": "RM3 Code", "fieldname": "rm3_code", "fieldtype": "Data"},
            {"label": "RM3 L1", "fieldname": "rm3_l1", "fieldtype": "Data"},
            {"label": "RM3 L2", "fieldname": "rm3_l2", "fieldtype": "Data"},
            {"label": "RM3 Qty", "fieldname": "rm3_qty", "fieldtype": "Float"},
            {"label": "B Side Rail L1", "fieldname": "b_side_rail_l1", "fieldtype": "Data"},
            {"label": "B Side Rail L2", "fieldname": "b_side_rail_l2", "fieldtype": "Data"},
            {"label": "B Side Rail Qty", "fieldname": "b_side_rail_qty", "fieldtype": "Float"},
            {"label": "Outer Cap L1", "fieldname": "outer_cap_l1", "fieldtype": "Data"},
            {"label": "Outer Cap L2", "fieldname": "outer_cap_l2", "fieldtype": "Data"},
            {"label": "Outer Cap Qty", "fieldname": "outer_cap_qty", "fieldtype": "Float"},
            {"label": "Rocker L1", "fieldname": "rocker_l1", "fieldtype": "Data"},
            {"label": "Rocker L2", "fieldname": "rocker_l2", "fieldtype": "Data"},
            {"label": "Rocker Qty", "fieldname": "rocker_qty", "fieldtype": "Float"},
            {"label": "Round Pipe L1", "fieldname": "round_pipe_l1", "fieldtype": "Data"},
            {"label": "Round Pipe L2", "fieldname": "round_pipe_l2", "fieldtype": "Data"},
            {"label": "Round Pipe Qty", "fieldname": "round_pipe_qty", "fieldtype": "Float"},
            {"label": "Square Pipe L1", "fieldname": "square_pipe_l1", "fieldtype": "Data"},
            {"label": "Square Pipe L2", "fieldname": "square_pipe_l2", "fieldtype": "Data"},
            {"label": "Square Pipe Qty", "fieldname": "square_pipe_qty", "fieldtype": "Float"},
            {"label": "Stiffner H L1", "fieldname": "stiffner_h_l1", "fieldtype": "Data"},
            {"label": "Stiffner H L2", "fieldname": "stiffner_h_l2", "fieldtype": "Data"},
            {"label": "Stiffner H Qty", "fieldname": "stiffner_h_qty", "fieldtype": "Float"},
            {"label": "Stiffner I L1", "fieldname": "stiffner_i_l1", "fieldtype": "Data"},
            {"label": "Stiffner I L2", "fieldname": "stiffner_i_l2", "fieldtype": "Data"},
            {"label": "Stiffner I Qty", "fieldname": "stiffner_i_qty", "fieldtype": "Float"},
            {"label": "Stiffner Plate L1", "fieldname": "stiffner_plate_l1", "fieldtype": "Data"},
            {"label": "Stiffner Plate L2", "fieldname": "stiffner_plate_l2", "fieldtype": "Data"},
            {"label": "Stiffner Plate Qty", "fieldname": "stiffner_plate_qty", "fieldtype": "Float"},
            {"label": "Stiffner U L1", "fieldname": "stiffner_u_l1", "fieldtype": "Data"},
            {"label": "Stiffner U L2", "fieldname": "stiffner_u_l2", "fieldtype": "Data"},
            {"label": "Stiffner U Qty", "fieldname": "stiffner_u_qty", "fieldtype": "Float"},
        ]
        
        # Remove columns with no data across all rows
        always_keep = {"fg_code", "project", "ipo_name", "section", "a", "b", "code", "l1", "l2", "dwg_no", "total_quantity", "u_area", "total_area"}
        columns_to_keep = []
        for col in columns:
            field = col["fieldname"]
            if field in always_keep:
                columns_to_keep.append(col)
                continue
            has_value = any(row.get(field) not in [None, "", 0, 0.0] for row in data)
            if has_value:
                columns_to_keep.append(col)
            else:
                for row in data:
                    row.pop(field, None)
        columns = columns_to_keep

        return columns, data

    except Exception as e:
        frappe.log_error(
            message=f"Report execution error: {str(e)}\nTraceback: {frappe.get_traceback()}",
            title="Report Execution Error"
        )
        frappe.throw(f"Error generating report: {str(e)}")
