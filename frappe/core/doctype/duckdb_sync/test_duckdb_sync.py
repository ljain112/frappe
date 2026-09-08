# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from datetime import timedelta

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype
from frappe.core.doctype.duckdb_sync.duckdb_sync import sync_data_to_duckdb
from frappe.database import delete_duckdb_file
from frappe.database.duckdb.database import snapshot
from frappe.query_builder.functions import DateFormat, GroupConcat, Match, Sum
from frappe.query_builder.utils import db_type_is, get_query_target, prepare_query
from frappe.tests import IntegrationTestCase

EXTRA_TEST_RECORD_DEPENDENCIES = []
IGNORE_TEST_RECORD_DEPENDENCIES = []


class IntegrationTestDuckDBSync(IntegrationTestCase):
	"""Sync a real doctype, then drive queries through the snapshot the way a report does.

	Everything here goes through the real entry points -- a submitted `DuckDB Sync`, the actual
	sync job, and `frappe.qb` / `frappe.get_list` inside `snapshot()`. The load-bearing assertion
	throughout is that the snapshot answers *identically* to the site database.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		# our own doctype, so the assertions do not depend on whatever data a site happens to hold
		cls.doctype = (
			new_doctype(
				fields=[
					{"label": "Title", "fieldname": "title", "fieldtype": "Data"},
					{"label": "Role", "fieldname": "role", "fieldtype": "Link", "options": "Role"},
					{"label": "Amount", "fieldname": "amount", "fieldtype": "Currency"},
					{"label": "Start Time", "fieldname": "start_time", "fieldtype": "Time"},
				],
			)
			.insert()
			.name
		)

		cls.roles = ["System Manager", "Guest"]
		for i in range(6):
			frappe.get_doc(
				{
					"doctype": cls.doctype,
					# mixed case on purpose: MariaDB LIKE is case-insensitive, DuckDB's is not
					"title": f"Sample {i}",
					"role": cls.roles[i % 2],
					"amount": 10 * (i + 1),
					"start_time": f"1{i}:30:00",
				}
			).insert()

		sync = frappe.get_doc({"doctype": "DuckDB Sync", "doc_type": cls.doctype}).insert()
		sync.submit()
		cls.addClassCleanup(delete_duckdb_file, sync.filename)

		for _ in range(20):
			if not frappe.db.exists("DuckDB Sync Item", {"parent": sync.name, "synced": 0}):
				break
			sync_data_to_duckdb(sync.name)
		else:
			raise AssertionError("DuckDB sync did not finish")

		cls.sync = sync

	def table(self):
		return frappe.qb.DocType(self.doctype)

	def render(self, query):
		"""Render `query` for the snapshot the way `execute_query` would."""
		return prepare_query(query, dialect=db_type_is.DUCKDB)

	def test_snapshot_returns_the_same_rows_as_the_site_database(self):
		dt = self.table()

		def query():
			return (
				frappe.qb.from_(dt)
				.select(dt.role, Sum(dt.amount).as_("total"))
				.groupby(dt.role)
				.orderby(dt.role)
			)

		expected = query().run(as_dict=True)
		self.assertTrue(expected, "fixture rows should be visible on the site database")

		with snapshot([self.doctype]):
			self.assertEqual(expected, query().run(as_dict=True))
			# every result shape Database.sql supports must match too
			self.assertEqual(query().run(), query().run())
			self.assertEqual(query().run(as_list=True), [list(row) for row in query().run()])
			self.assertEqual(query().run(pluck=True), [row[0] for row in query().run()])

	def test_like_stays_case_insensitive_on_the_snapshot(self):
		"""A plain LIKE re-rendered for DuckDB matches case-sensitively and silently drops rows."""
		dt = self.table()

		def query():
			return frappe.qb.from_(dt).select(dt.name).where(dt.title.like("SAMPLE%")).orderby(dt.name)

		expected = query().run()
		self.assertEqual(len(expected), 6, "uppercase pattern should still match on mariadb")

		with snapshot([self.doctype]):
			self.assertEqual(expected, query().run())

	def test_time_fields_keep_the_site_database_type(self):
		"""MariaDB returns a Time field as timedelta; pyarrow can only store it as a time."""
		dt = self.table()

		def query():
			return frappe.qb.from_(dt).select(dt.name, dt.start_time).orderby(dt.name)

		expected = query().run(as_dict=True)
		self.assertTrue(all(isinstance(row.start_time, timedelta) for row in expected))

		with snapshot([self.doctype]):
			self.assertEqual(expected, query().run(as_dict=True))

	def test_streamed_results_match_the_site_database(self):
		"""erpnext's accounts receivable streams its ledger query rather than buffering it."""
		dt = self.table()

		def query():
			return frappe.qb.from_(dt).select(dt.name, dt.amount).orderby(dt.name)

		expected = query().run(as_dict=True)
		expected_names = [row.name for row in expected]

		with snapshot([self.doctype]):
			self.assertEqual(expected, list(query().run(as_dict=True, as_iterator=True)))
			self.assertEqual(expected_names, list(query().run(pluck=True, as_iterator=True)))

	def test_grouped_query_matches_the_site_database(self):
		"""DuckDB enforces GROUP BY as strictly as postgres; mariadb does not.

		`get_all` adds a default `order by creation`, which mariadb accepts alongside a GROUP BY
		and DuckDB rejects -- this is what broke erpnext's sales register and item-wise sales
		register until the Engine started building these under the strict rules.
		"""
		fields = ["role", {"SUM": "amount"}]
		expected = frappe.get_all(self.doctype, fields=fields, group_by="role", order_by="role")
		self.assertTrue(expected)

		with snapshot([self.doctype]):
			self.assertEqual(
				expected, frappe.get_all(self.doctype, fields=fields, group_by="role", order_by="role")
			)

	def test_get_all_snapshot_kwarg_matches_the_site_database(self):
		"""`snapshot=True` reads one query from the snapshot without opening a block."""
		fields = ["role", {"SUM": "amount"}]
		expected = frappe.get_all(self.doctype, fields=fields, group_by="role", order_by="role")
		self.assertTrue(expected)

		self.assertEqual(
			expected,
			frappe.get_all(self.doctype, fields=fields, group_by="role", order_by="role", snapshot=True),
		)
		# and it is scoped to the one call -- the next read is back on the site database
		self.assertIsNone(getattr(frappe.local, "query_targets", None))

	def test_in_flight_sync_is_not_served_to_a_report(self):
		"""A sync is submitted before its rows exist, and each table is emptied before refilling.

		Serving the newest *submitted* sync therefore hands a report an empty or half-loaded file,
		which returns zeros instead of raising -- the worst failure mode for a financial report.
		Only a sync with no pending items may be served.
		"""
		dt = self.table()

		def query():
			return frappe.qb.from_(dt).select(dt.name).orderby(dt.name)

		expected = query().run()
		self.assertTrue(expected)

		# submitted, so on_submit has created its (empty) tables, but the load never runs
		in_flight = frappe.get_doc({"doctype": "DuckDB Sync", "doc_type": self.doctype}).insert()
		in_flight.submit()
		self.addCleanup(delete_duckdb_file, in_flight.filename)
		self.assertTrue(frappe.db.exists("DuckDB Sync Item", {"parent": in_flight.name, "synced": 0}))

		with snapshot([self.doctype]):
			self.assertEqual(expected, query().run())

	def test_join_across_two_synced_doctypes(self):
		"""Two snapshots are ATTACHed onto one connection, so a join across them is served here."""
		role = frappe.qb.DocType("Role")
		dt = self.table()

		def query():
			return (
				frappe.qb.from_(dt)
				.join(role)
				.on(role.name == dt.role)
				.select(dt.name, role.name.as_("role_name"))
				.orderby(dt.name)
			)

		expected = query().run(as_dict=True)
		self.assertTrue(expected)

		sync = frappe.get_doc({"doctype": "DuckDB Sync", "doc_type": "Role"}).insert()
		sync.submit()
		self.addCleanup(delete_duckdb_file, sync.filename)
		for _ in range(20):
			if not frappe.db.exists("DuckDB Sync Item", {"parent": sync.name, "synced": 0}):
				break
			sync_data_to_duckdb(sync.name)

		with snapshot([self.doctype, "Role"]) as targets:
			self.assertEqual(expected, query().run(as_dict=True))
			# and it really was the snapshot, not a fallback
			self.assertIsNotNone(get_query_target(query()))
			self.assertIn("tabRole", targets[0].tables)

	def test_query_on_unsynced_doctype_falls_back(self):
		with snapshot([self.doctype]):
			role = frappe.qb.DocType("Role")
			self.assertTrue(frappe.qb.from_(role).select(role.name).limit(1).run())
			self.assertTrue(frappe.get_all("Role", limit=1))
			self.assertTrue(frappe.db.get_value("Role", {"name": "System Manager"}, "name"))

	def test_subquery_on_unsynced_table_falls_back(self):
		"""The snapshot holds one doctype, so a filter against master data must not be routed to it.

		`ExistsCriterion` does not expose the query it wraps, so this cannot be caught by walking
		the query; it is the DuckDB catalog error that sends it back to the site database.
		"""
		from pypika.terms import ExistsCriterion

		dt, role = self.table(), frappe.qb.DocType("Role")

		def query():
			return (
				frappe.qb.from_(dt)
				.select(dt.name)
				.where(ExistsCriterion(frappe.qb.from_(role).select(role.name).where(role.name == dt.role)))
				.orderby(dt.name)
			)

		expected = query().run()
		with snapshot([self.doctype]):
			self.assertEqual(expected, query().run())

	def test_permissions_are_applied_on_the_snapshot(self):
		user = "duckdb_snapshot_permissions@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": user,
					"first_name": "DuckDB Snapshot",
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}],
				}
			).insert()

		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Role",
				"for_value": self.roles[0],
				"apply_to_all_doctypes": 1,
			}
		).insert()

		unrestricted = len(frappe.get_list(self.doctype, fields=["name"], limit_page_length=0))

		try:
			frappe.set_user(user)
			expected = frappe.get_list(self.doctype, fields=["name"], limit_page_length=0, order_by="name")
			with snapshot([self.doctype]):
				self.assertEqual(
					expected,
					frappe.get_list(self.doctype, fields=["name"], limit_page_length=0, order_by="name"),
				)
		finally:
			frappe.set_user("Administrator")

		# a permission that filtered nothing would make the assertion above vacuous
		self.assertLess(len(expected), unrestricted)

	def test_values_are_bound_not_interpolated(self):
		dt = self.table()
		hostile = "x' OR '1'='1"

		with snapshot([self.doctype]):
			query = frappe.qb.from_(dt).select(dt.name).where(dt.title == hostile)
			sql, params, _ = self.render(query)

			self.assertNotIn(hostile, sql)
			self.assertIn("$param1", sql)
			self.assertEqual(list(params.values()), [hostile])
			self.assertFalse(query.run())

	def test_dialect_functions_render_in_duckdb_spelling(self):
		dt = self.table()

		with snapshot([self.doctype]):
			# DuckDB has neither to_char nor date_format; strftime takes the same % codes
			sql, *_ = self.render(frappe.qb.from_(dt).select(DateFormat(dt.creation, "%Y-%m")))
			self.assertIn("strftime", sql.lower())

			# REGEXP is a parser error and ~* an unknown function on DuckDB
			sql, *_ = self.render(frappe.qb.from_(dt).select(dt.name).where(dt.title.regex("Sample")))
			self.assertIn("regexp_matches", sql.lower())

			# FORCE INDEX is MySQL-only and a syntax error on DuckDB
			sql, *_ = self.render(frappe.qb.from_(dt).select(dt.name).force_index("creation"))
			self.assertNotIn("INDEX", sql.upper())

	def test_chained_builder_state_survives_dialect_rendering(self):
		"""`.distinct()` is @builder state on the term a dialect rendering replaces.

		Dropping it does not raise -- it silently widens the result -- and erpnext's pick list
		report chains exactly this.
		"""
		dt = self.table()

		sql, *_ = self.render(frappe.qb.from_(dt).select(GroupConcat(dt.title).distinct()))
		self.assertIn("DISTINCT", sql)

		sql, *_ = self.render(frappe.qb.from_(dt).select(GroupConcat(dt.title)))
		self.assertNotIn("DISTINCT", sql)

	def test_fulltext_search_is_rejected_on_a_snapshot(self):
		dt = self.table()

		with snapshot([self.doctype]):
			query = frappe.qb.from_(dt).select(dt.name).where(Match(dt.title).Against("sample"))
			self.assertRaises(frappe.ValidationError, self.render, query)
