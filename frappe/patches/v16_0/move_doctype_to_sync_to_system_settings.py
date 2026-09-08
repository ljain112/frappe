import frappe


def execute():
	"""Move the snapshot sync list off individual Reports and onto System Settings.

	Which doctypes are replicated to DuckDB is a site-level operational choice -- it depends on
	data volume -- so it is declared once rather than repeated on every snapshot report. Reports
	keep only the `snapshot_report` flag.
	"""
	if not frappe.db.table_exists("Doctype To Sync"):
		return

	doctypes = frappe.get_all(
		"Doctype To Sync",
		filters={"parenttype": "Report", "parentfield": "doctype_to_sync"},
		pluck="doc_type",
		distinct=True,
	)
	if not doctypes:
		return

	settings = frappe.get_single("System Settings")
	existing = {row.doc_type for row in settings.doctype_to_sync}

	for doctype in doctypes:
		if doctype not in existing:
			settings.append("doctype_to_sync", {"doc_type": doctype})

	settings.flags.ignore_validate = True
	settings.save(ignore_permissions=True)

	frappe.db.delete("Doctype To Sync", {"parenttype": "Report", "parentfield": "doctype_to_sync"})
