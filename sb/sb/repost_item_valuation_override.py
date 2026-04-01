"""
Extend ERPNext Repost Item Valuation so SLE custom_length is restored after repost.

Stock repost rebuilds Stock Ledger Entry rows without app custom fields; the
on_submit hook runs before that repost, so lengths were always lost.
"""

import frappe
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
    RepostItemValuation as ERPNextRepostItemValuation,
)

from sb.sb.stock_hooks import apply_custom_length_to_sle


class RepostItemValuation(ERPNextRepostItemValuation):
    def set_status(self, status=None, write=True):
        super().set_status(status, write)
        if not write or self.status != "Completed":
            return
        if self.based_on != "Transaction":
            return
        if self.voucher_type not in ("Stock Entry", "Purchase Receipt"):
            return
        if not self.voucher_no:
            return
        try:
            voucher = frappe.get_doc(self.voucher_type, self.voucher_no)
            if voucher.docstatus != 1:
                return
            apply_custom_length_to_sle(voucher)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"restore SLE length after repost ({self.voucher_type} {self.voucher_no})",
            )
