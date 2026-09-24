# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import difflib
import json

import frappe
from frappe.model import datetime_fields, no_value_fields, table_fields
from frappe.model.document import Document, bulk_insert
from frappe.utils import cstr

FIELDTYPES_TO_IGNORE = frozenset(fieldtype for fieldtype in no_value_fields if fieldtype not in table_fields)


class Version(Document):
	_DOCTYPE_NAME = "Version"

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		data: DF.Code | None
		docname: DF.Data
		ref_doctype: DF.Link
	# end: auto-generated types

	def update_version_info(
		self, old: Document | None, new: Document, *, fieldnames: set[str] | None = None
	) -> bool:
		"""Update changed info and return true if change contains useful data.

		:param fieldnames: Compare only these fields (and, in child rows, their fields), where only they can differ.
		"""
		if not old:
			# Check if doc has some information about creation source like data import
			return self.for_insert(new)
		else:
			return self.set_diff(old, new, fieldnames=fieldnames)

	@staticmethod
	def set_impersonator(data):
		if not frappe.session:
			return
		if impersonator := frappe.session.data.get("impersonated_by"):
			data["impersonated_by"] = impersonator

		if audit_user := frappe.session.data.get("audit_user"):
			data["audit_user"] = audit_user

	def set_diff(self, old: Document, new: Document, *, fieldnames: set[str] | None = None) -> bool:
		"""Set the data property with the diff of the docs if present"""
		diff = get_diff(old, new, include_ignored_fields=False, fieldnames=fieldnames)
		if diff:
			self.set_impersonator(diff)
			self.ref_doctype = new.doctype
			self.docname = new.name
			self.data = frappe.as_json(diff, indent=None, separators=(",", ":"))
			return True
		else:
			return False

	def for_insert(self, doc: Document) -> bool:
		updater_reference = doc.flags.updater_reference
		if not updater_reference:
			return False

		data = {
			"creation": doc.creation,
			"updater_reference": updater_reference,
			"created_by": doc.owner,
		}
		self.set_impersonator(data)
		self.ref_doctype = doc.doctype
		self.docname = doc.name
		self.data = frappe.as_json(data, indent=None, separators=(",", ":"))
		return True

	def get_data(self):
		return json.loads(self.data)

	def onload(self):
		"""Generate HTML diffs for multiline changes on document load."""
		if not self.data:
			return

		data = self.get_data()
		changed = data.get("changed", [])
		if not changed:
			return

		html_diffs = {}
		for item in changed:
			if len(item) >= 3:
				fieldname, old_str, new_str = item[0], _as_string(item[1]), _as_string(item[2])
				if not _should_generate_html_diff(old_str, new_str):
					continue
				html_diff = _generate_html_diff(old_str, new_str)
				if html_diff:
					html_diffs[fieldname] = html_diff

		if html_diffs:
			self.set_onload("html_diffs", html_diffs)


def get_diff(
	old, new, for_child=False, compare_cancelled=False, include_ignored_fields=True, *, fieldnames=None
):
	"""Get diff between 2 document objects

	`fieldnames` limits the comparison to those fields, in the document and in its child rows, for a
	caller that knows no other field can differ.

	If there is a change, then returns a dict like:

	        {
	                "changed"    : [[fieldname1, old, new], [fieldname2, old, new]],
	                "added"      : [[table_fieldname1, {dict}], ],
	                "removed"    : [[table_fieldname1, {dict}], ],
	                "row_changed": [[table_fieldname1, row_name1, row_index,
	                        [[child_fieldname1, old, new],
	                        [child_fieldname2, old, new]], ]
	                ],

	        }"""

	def get_row_data(row) -> dict:
		"""
		Row data for the version log, without fields set to `Ignore Versioning`.
		"""
		data = row.as_dict()

		if include_ignored_fields:
			return data

		for fieldname in row.meta.ignore_versioning_fields:
			data.pop(fieldname, None)

		return data

	if not new:
		return None

	ignored_fields = set() if include_ignored_fields else new.meta.ignore_versioning_fields

	blacklisted_fields = ["Markdown Editor", "Text Editor", "Code", "HTML Editor"]

	# capture data import if set
	data_import = new.flags.via_data_import
	updater_reference = new.flags.updater_reference

	out = frappe._dict(
		changed=[],
		added=[],
		removed=[],
		row_changed=[],
		data_import=data_import,
		updater_reference=updater_reference,
	)

	if not for_child:
		amended_from = new.get("amended_from")
		old_row_name_field = "_amended_from" if (amended_from and amended_from == old.name) else "name"

	for df in new.meta.fields:
		if fieldnames is not None and df.fieldname not in fieldnames:
			continue

		if df.fieldtype in FIELDTYPES_TO_IGNORE or getattr(df, "is_virtual", False):
			continue

		if df.fieldname in ignored_fields:
			continue

		old_value, new_value = old.get(df.fieldname), new.get(df.fieldname)
		if df.fieldtype in ("Link", "Dynamic Link"):
			old_value, new_value = cstr(old_value), cstr(new_value)

		if df.fieldtype in datetime_fields:
			if old_value is None and new_value == "":
				new_value = None

		if not for_child and df.fieldtype in table_fields:
			old_rows_by_name = {}
			for d in old_value:
				old_rows_by_name[d.name] = d

			found_rows = set()

			# check rows for additions, changes
			for i, d in enumerate(new_value):
				old_row_name = getattr(d, old_row_name_field, None)
				if compare_cancelled:
					if amended_from:
						if len(old_value) > i:
							old_row_name = old_value[i].name

				if old_row_name and old_row_name in old_rows_by_name:
					found_rows.add(old_row_name)

					diff = get_diff(
						old_rows_by_name[old_row_name],
						d,
						for_child=True,
						include_ignored_fields=include_ignored_fields,
						fieldnames=fieldnames,
					)
					if diff and diff.changed:
						out.row_changed.append((df.fieldname, i, d.name, diff.changed))
				else:
					out.added.append([df.fieldname, get_row_data(d)])

			# check for deletions
			for d in old_value:
				if d.name not in found_rows:
					out.removed.append([df.fieldname, get_row_data(d)])

		elif old_value != new_value:
			if df.fieldtype not in blacklisted_fields:
				old_value = old.get_formatted(df.fieldname) if old_value else old_value
				new_value = new.get_formatted(df.fieldname) if new_value else new_value

			if old_value != new_value:
				doctype = new.doctype or old.doctype
				if doctype:
					meta = frappe.get_meta(doctype)

					if (field_meta := meta.get_field(df.fieldname)) and field_meta.fieldtype == "Link":
						link_meta = frappe.get_meta(field_meta.options)

						# Show title field value if field is Link and show_title_field_in_link is True
						if link_meta.show_title_field_in_link and (
							(title_field := link_meta.get_title_field()) != "name"
						):
							old_title_val, new_title_val = "", ""
							result = frappe.db.get_values(
								field_meta.options,
								{"name": ("in", (old_value, new_value))},
								["name", title_field],
							)
							for r in result:
								if r[0] == old_value:
									old_title_val = r[1]
								elif r[0] == new_value:
									new_title_val = r[1]
							out.changed.append((df.fieldname, old_title_val, new_title_val))
							continue
				out.changed.append((df.fieldname, old_value, new_value))

	# name & docstatus
	if not for_child:
		for key in ("name", "docstatus"):
			old_value = getattr(old, key)
			new_value = getattr(new, key)

			if old_value != new_value:
				out.changed.append([key, old_value, new_value])

	if any((out.changed, out.added, out.removed, out.row_changed)):
		return out

	else:
		return None


def on_doctype_update():
	frappe.db.add_index("Version", ["ref_doctype", "docname"])


def _generate_html_diff(old_str: str, new_str: str) -> str | None:
	"""Generate HTML diff for the given old and new strings."""
	old_lines = old_str.splitlines(keepends=True)
	new_lines = new_str.splitlines(keepends=True)

	differ = difflib.HtmlDiff(wrapcolumn=80)
	html_diff = differ.make_table(
		old_lines,
		new_lines,
		fromdesc=frappe._("Original"),
		todesc=frappe._("New"),
		context=True,
		numlines=3,
	)
	return html_diff


def _should_generate_html_diff(old_str: str, new_str: str) -> bool:
	"""Determine if HTML diff should be generated for the given values."""
	return (
		old_str and new_str and ("\n" in old_str or "\n" in new_str or len(old_str) > 80 or len(new_str) > 80)
	)


def _as_string(value: str | None) -> str:
	"""Convert the given value to a string."""
	return cstr(value) if value is not None else ""


def tracks_changes(meta) -> bool:
	"""Whether changes to documents of this DocType are recorded as Versions (`Document._get_version` asks too)."""
	return (
		bool(getattr(meta, "track_changes", False)) and meta.name != "Version" and not frappe.flags.in_install
	)


def prepare_versions(doctype: str, doc_updates: dict, updater_reference: dict | None = None) -> list[Version]:
	"""Return the unsaved Versions that record `doc_updates` to `doctype`, as `Document.save` records them.

	For a write made without the documents (`frappe.db.set_value`, `bulk_update`): call it before the write,
	which changes the values read here, and `insert_versions` after. Each document is read whole, in one
	query, so the diff and its formatting see every field; of its child tables, it holds only those with
	the updated rows. Rows of a child DocType are recorded on their parent documents.

	:param doc_updates: `{name: {fieldname: value}}`
	:param updater_reference: `{"doctype", "docname", "label"}` of what made the change.
	"""
	meta = frappe.get_meta(doctype)
	# rows of a child DocType are recorded on their parents, which decide
	if not meta.istable and not tracks_changes(meta):
		return []

	doc_updates = {cstr(name): values for name, values in doc_updates.items()}
	fieldnames = {fieldname for values in doc_updates.values() for fieldname in values}

	def whole_rows(doctype, meta, names):
		if meta.issingle:
			return [frappe._dict(frappe.db.get_singles_dict(doctype, cast=True), name=doctype)]
		return frappe.get_all(doctype, filters={"name": ("in", list(names))}, fields=["*"])

	documents = []  # (as it is, as it will be)
	if meta.istable:
		parents = {}
		for row in frappe.get_all(
			doctype, filters={"name": ("in", list(doc_updates))}, fields=["parent", "parenttype"]
		):
			if tracks_changes(frappe.get_meta(row.parenttype)):
				parents.setdefault(row.parenttype, set()).add(row.parent)

		for parenttype, parent_names in parents.items():
			# the tables that hold the rows, whole and in order, so that a row is recorded at its index
			parent_meta = frappe.get_meta(parenttype)
			table_fieldnames = [
				df.fieldname for df in parent_meta.get_table_fields() if df.options == doctype
			]
			fieldnames |= set(table_fieldnames)  # the diff walks these tables to the fields of their rows
			tables = {}
			for row in frappe.get_all(
				doctype,
				filters={"parent": ("in", list(parent_names)), "parenttype": parenttype},
				fields=["*"],
				order_by="idx asc",
			):
				tables.setdefault((row.parent, row.parentfield), []).append(row)

			for parent in whole_rows(parenttype, parent_meta, parent_names):
				rows = {f: tables.get((parent.name, f), []) for f in table_fieldnames}
				documents.append(
					(
						frappe.get_doc({**parent, "doctype": parenttype, **rows}),
						frappe.get_doc(
							{
								**parent,
								"doctype": parenttype,
								**{
									f: [{**row, **doc_updates.get(row.name, {})} for row in table]
									for f, table in rows.items()
								},
							}
						),
					)
				)
	else:
		for row in whole_rows(doctype, meta, doc_updates):
			updated = {**row, **doc_updates[cstr(row.name)], "doctype": doctype}
			documents.append((frappe.get_doc({**row, "doctype": doctype}), frappe.get_doc(updated)))

	versions = []
	for before, after in documents:
		after._doc_before_save = before
		after.flags.updater_reference = updater_reference
		if version := after._get_version(fieldnames=fieldnames):
			version.set_new_name()
			versions.append(version)

	return versions


def insert_versions(versions: list[Version]) -> None:
	"""Insert, in one query, the Versions from `prepare_versions`, after the write they record."""
	if versions:
		bulk_insert("Version", versions)
