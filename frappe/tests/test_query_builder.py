import unittest
from collections.abc import Callable
from datetime import time

from pypika import Dialects
from pypika.functions import Cast
from pypika.terms import ValueWrapper

import frappe
from frappe.core.doctype.doctype.test_doctype import new_doctype
from frappe.database.operator_map import OPERATOR_MAP, func_in
from frappe.query_builder import Case
from frappe.query_builder.builder import Function
from frappe.query_builder.custom import ConstantColumn, MonthName, Year
from frappe.query_builder.functions import (
	Cast_,
	Coalesce,
	CombineDatetime,
	CurDate,
	Date,
	DateDiff,
	DateFormat,
	GroupConcat,
	IsNotSet,
	IsSet,
	JSONContains,
	JSONExtract,
	JSONValue,
	Locate,
	Match,
	Month,
	NameAsText,
	Quarter,
	Round,
	TextLike,
	Timestamp,
	Truncate,
	UnixTimestamp,
	YearWeek,
)
from frappe.query_builder.utils import (
	DB_TYPES,
	MAX_LIMIT,
	SHAPE_RULES,
	DbType,
	PseudoColumnMapper,
	QueryShape,
	UnsupportedOperation,
	compile_query,
	db_type_is,
	per_db_type,
)
from frappe.tests import IntegrationTestCase


def run_only_if(dbtype: db_type_is) -> Callable:
	return unittest.skipIf(db_type_is(frappe.conf.db_type) != dbtype, f"Only runs for {dbtype.value}")


def unimplemented_for(*dbtypes: db_type_is) -> Callable:
	current_db_type = db_type_is(frappe.conf.db_type)
	return unittest.skipIf(current_db_type in dbtypes, f"Not Implemented for {current_db_type.value}")


@run_only_if(db_type_is.MARIADB)
class TestCustomFunctionsMariaDB(IntegrationTestCase):
	def test_concat(self):
		self.assertEqual("GROUP_CONCAT('Notes' SEPARATOR ',')", GroupConcat("Notes").get_sql())
		self.assertEqual("GROUP_CONCAT('Notes' SEPARATOR ', ')", GroupConcat("Notes", ", ").get_sql())
		user = frappe.qb.DocType("User")
		query = frappe.qb.from_(user).select(GroupConcat(user.email).separator(" | ").as_("user_list"))
		sql = query.get_sql()
		self.assertIn("SEPARATOR ' | '", sql)
		self.assertIn("`user_list`", sql)

	def test_concat_alias_escapes_the_quote_char(self):
		# The alias goes through format_alias_sql like every other term, so the quote char inside
		# it is doubled. Rendering it raw would close the identifier early, and would disagree
		# with the escaped alias pypika emits for the same term in GROUP BY / ORDER BY.
		user = frappe.qb.DocType("User")
		gc = GroupConcat(user.email).as_("a`b")
		sql = frappe.qb.from_(user).select(gc).groupby(gc).get_sql()
		self.assertIn("`a``b`", sql)
		self.assertNotIn("`a`b`", sql)
		# the alias SELECT declares is the one GROUP BY refers to
		self.assertEqual(2, sql.count("`a``b`"))

	def test_concat_alias_only_in_select_position(self):
		# an alias is part of the select clause, not of the expression: appending it in operand
		# position produces `GROUP_CONCAT(...) `x` LIKE ...`, which is a syntax error
		user = frappe.qb.DocType("User")
		gc = GroupConcat(user.email).as_("user_list")
		sql = frappe.qb.from_(user).select(user.name).where(gc.like("%admin%")).get_sql()
		self.assertNotIn("`user_list`", sql)
		self.assertIn("GROUP_CONCAT(`email` SEPARATOR ',') LIKE", sql)

	def test_concat_with_explicit_empty_separator(self):
		# "" means "no delimiter", not "use the default" -- dropping the clause would silently
		# fall back to MariaDB's comma while postgres STRING_AGG concatenates bare.
		self.assertEqual("GROUP_CONCAT('Notes' SEPARATOR '')", GroupConcat("Notes", "").get_sql())
		self.assertEqual("GROUP_CONCAT('Notes' SEPARATOR '')", GroupConcat("Notes").separator("").get_sql())

	def test_like_keeps_native_operator(self):
		# MariaDB LIKE is already case-insensitive; keep the native operator
		user = frappe.qb.DocType("User")
		sql = frappe.qb.from_(user).select(user.name).where(user.name.like("%admin%")).get_sql()
		self.assertIn("LIKE", sql)
		self.assertNotIn("ILIKE", sql)
		not_sql = frappe.qb.from_(user).select(user.name).where(user.name.not_like("%admin%")).get_sql()
		self.assertIn("NOT LIKE", not_sql)

	def test_match(self):
		query = Match("Notes")
		with self.assertRaises(Exception):
			query.get_sql()
		query = query.Against("text")
		self.assertEqual(" MATCH('Notes') AGAINST ('+text*' IN BOOLEAN MODE)", query.get_sql())

	def test_constant_column(self):
		query = frappe.qb.from_("DocType").select("name", ConstantColumn("John").as_("User"))
		self.assertEqual(query.get_sql(), "SELECT `name`,'John' `User` FROM `tabDocType`")

	def test_timestamp(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"TIMESTAMP(posting_date,posting_time)",
			CombineDatetime(note.posting_date, note.posting_time).get_sql(),
		)
		self.assertEqual(
			"TIMESTAMP('2021-01-01','00:00:21')", CombineDatetime("2021-01-01", "00:00:21").get_sql()
		)

		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(CombineDatetime(note.posting_date, note.posting_time))
		)
		self.assertIn(
			"select timestamp(`tabnote`.`posting_date`,`tabnote`.`posting_time`)", str(select_query).lower()
		)

		select_query = select_query.orderby(CombineDatetime(note.posting_date, note.posting_time))
		self.assertIn(
			"order by timestamp(`tabnote`.`posting_date`,`tabnote`.`posting_time`)",
			str(select_query).lower(),
		)

		select_query = select_query.where(
			CombineDatetime(note.posting_date, note.posting_time) >= CombineDatetime("2021-01-01", "00:00:01")
		)
		self.assertIn(
			"timestamp(`tabnote`.`posting_date`,`tabnote`.`posting_time`)>=timestamp('2021-01-01','00:00:01')",
			str(select_query).lower(),
		)

		select_query = select_query.select(
			CombineDatetime(note.posting_date, note.posting_time, alias="timestamp")
		)
		self.assertIn(
			"timestamp(`tabnote`.`posting_date`,`tabnote`.`posting_time`) `timestamp`",
			str(select_query).lower(),
		)

	def test_curdate(self):
		# CURRENT_DATE must render as a bare keyword (no parentheses) so it is valid on postgres too.
		self.assertEqual("CURRENT_DATE", CurDate().get_sql())
		note = frappe.qb.DocType("Note")
		query = frappe.qb.from_(note).select(note.name).where(note.posting_date >= CurDate())
		self.assertIn("current_date", str(query).lower())
		self.assertNotIn("current_date(", str(query).lower())

	def test_month_quarter_mariadb(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual("MONTH(posting_date)", Month(note.posting_date).get_sql())
		self.assertEqual("QUARTER(posting_date)", Quarter(note.posting_date).get_sql())

	def test_unix_ts_mariadb(self):
		# Simple Query
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"unix_timestamp(posting_date)",
			UnixTimestamp(note.posting_date).get_sql(),
		)

		# Complex multi table query
		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(UnixTimestamp(note.posting_date))
		)
		self.assertIn("select unix_timestamp(`tabnote`.`posting_date`)", str(select_query).lower())

		# Order by
		select_query = select_query.orderby(UnixTimestamp(note.posting_date))
		self.assertIn(
			"order by unix_timestamp(`tabnote`.`posting_date`)",
			str(select_query).lower(),
		)

		# Function comparison
		select_query = select_query.where(UnixTimestamp(note.posting_date) >= UnixTimestamp("2021-01-01"))
		self.assertIn(
			"unix_timestamp(`tabnote`.`posting_date`)>=unix_timestamp('2021-01-01')",
			str(select_query).lower(),
		)

		# aliasing
		select_query = select_query.select(UnixTimestamp(note.posting_date, alias="unix_ts"))
		self.assertIn(
			"unix_timestamp(`tabnote`.`posting_date`) `unix_ts`",
			str(select_query).lower(),
		)

	def test_datediff_mariadb(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"DATEDIFF(posting_date,creation)",
			DateDiff(note.posting_date, note.creation).get_sql(),
		)

		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(DateDiff(note.posting_date, note.creation))
		)
		self.assertIn(
			"select datediff(`tabnote`.`posting_date`,`tabnote`.`creation`)",
			str(select_query).lower(),
		)

	def test_time(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"TIMESTAMP('2021-01-01','00:00:21')", CombineDatetime("2021-01-01", time(0, 0, 21)).get_sql()
		)

		select_query = frappe.qb.from_(note).select(CombineDatetime(note.posting_date, note.posting_time))
		self.assertIn("select timestamp(`posting_date`,`posting_time`)", str(select_query).lower())

		select_query = select_query.where(
			CombineDatetime(note.posting_date, note.posting_time)
			>= CombineDatetime("2021-01-01", time(0, 0, 1))
		)
		self.assertIn(
			"timestamp(`posting_date`,`posting_time`)>=timestamp('2021-01-01','00:00:01')",
			str(select_query).lower(),
		)

	def test_cast(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual("CONCAT(name,'')", Cast_(note.name, "varchar").get_sql())
		self.assertEqual("CAST(name AS INTEGER)", Cast_(note.name, "integer").get_sql())
		self.assertEqual(
			frappe.qb.from_("red").from_(note).select("other", Cast_(note.name, "varchar")).get_sql(),
			"SELECT `tabred`.`other`,CONCAT(`tabNote`.`name`,'') FROM `tabred`,`tabNote`",
		)

	def test_round(self):
		note = frappe.qb.DocType("Note")

		query = frappe.qb.from_(note).select(Round(note.price))
		self.assertEqual("select round(`price`,0) from `tabnote`", str(query).lower())

		query = frappe.qb.from_(note).select(Round(note.price, 3))
		self.assertEqual("select round(`price`,3) from `tabnote`", str(query).lower())

	def test_truncate(self):
		note = frappe.qb.DocType("Note")
		query = frappe.qb.from_(note).select(Truncate(note.price, 3))
		self.assertEqual("select truncate(`price`,3) from `tabnote`", str(query).lower())

	def test_json_extract(self):
		note = frappe.qb.DocType("Note")
		# Simple get_sql
		self.assertEqual("JSON_EXTRACT(content,'$.key')", JSONExtract(note.content, "$.key").get_sql())

		# In a SELECT query
		query = frappe.qb.from_(note).select(JSONExtract(note.content, "$.key"))
		self.assertIn("json_extract(`content`,'$.key')", str(query).lower())

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONExtract(note.content, "$.key") == "value")
		self.assertIn("json_extract(`content`,'$.key')='value'", str(query).lower())

	def test_json_value(self):
		note = frappe.qb.DocType("Note")
		# Simple get_sql
		self.assertEqual(
			"JSON_UNQUOTE(JSON_EXTRACT(content,'$.key'))", JSONValue(note.content, "$.key").get_sql()
		)

		# In a SELECT query
		query = frappe.qb.from_(note).select(JSONValue(note.content, "$.key"))
		self.assertIn("json_unquote(json_extract(`content`,'$.key'))", str(query).lower())

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONValue(note.content, "$.key") == "value")
		self.assertIn("json_unquote(json_extract(`content`,'$.key'))='value'", str(query).lower())

	def test_json_contains(self):
		note = frappe.qb.DocType("Note")
		# With a plain string candidate (auto-wrapped as JSON)
		self.assertEqual("JSON_CONTAINS(content,'\"value\"')", JSONContains(note.content, "value").get_sql())

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONContains(note.content, "admin"))
		self.assertIn("json_contains(`content`,'\"admin\"')", str(query).lower())


@run_only_if(db_type_is.POSTGRES)
class TestCustomFunctionsPostgres(IntegrationTestCase):
	def test_concat(self):
		self.assertEqual("STRING_AGG('Notes',',')", GroupConcat("Notes").get_sql())
		self.assertEqual("STRING_AGG('Notes',', ')", GroupConcat("Notes", ", ").get_sql())
		# .separator() chaining must work on postgres too (STRING_AGG has no native SEPARATOR keyword)
		self.assertEqual("STRING_AGG('Notes',' | ')", GroupConcat("Notes").separator(" | ").get_sql())

	def test_concat_with_explicit_empty_separator(self):
		# must mean the same thing as the MariaDB rendering: no delimiter at all
		self.assertEqual("STRING_AGG('Notes','')", GroupConcat("Notes", "").get_sql())
		self.assertEqual("STRING_AGG('Notes','')", GroupConcat("Notes").separator("").get_sql())

	def test_like_is_case_insensitive(self):
		# postgres LIKE is case-sensitive; render ILIKE so search matches MariaDB's case-insensitivity
		user = frappe.qb.DocType("User")
		self.assertIn(
			"ILIKE", frappe.qb.from_(user).select(user.name).where(user.name.like("%admin%")).get_sql()
		)
		self.assertIn(
			"NOT ILIKE",
			frappe.qb.from_(user).select(user.name).where(user.name.not_like("%admin%")).get_sql(),
		)

	def test_match(self):
		# 'english' regconfig is pinned so a GIN index over to_tsvector('english', col) can back it
		query = Match("Notes")
		self.assertEqual("TO_TSVECTOR('english','Notes')", query.get_sql())
		query = Match("Notes").Against("text")
		self.assertEqual(
			"TO_TSVECTOR('english','Notes') @@ PLAINTO_TSQUERY('english', 'text')", query.get_sql()
		)

	def test_constant_column(self):
		query = frappe.qb.from_("DocType").select("name", ConstantColumn("John").as_("User"))
		self.assertEqual(query.get_sql(), 'SELECT "name",\'John\' "User" FROM "tabDocType"')

	def test_timestamp(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"posting_date+posting_time", CombineDatetime(note.posting_date, note.posting_time).get_sql()
		)
		self.assertEqual(
			"CAST('2021-01-01' AS DATE)+CAST('00:00:21' AS TIME)",
			CombineDatetime("2021-01-01", "00:00:21").get_sql(),
		)

		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(CombineDatetime(note.posting_date, note.posting_time))
		)
		self.assertIn('select "tabnote"."posting_date"+"tabnote"."posting_time"', str(select_query).lower())

		select_query = select_query.orderby(CombineDatetime(note.posting_date, note.posting_time))
		self.assertIn('order by "tabnote"."posting_date"+"tabnote"."posting_time"', str(select_query).lower())

		select_query = select_query.where(
			CombineDatetime(note.posting_date, note.posting_time) >= CombineDatetime("2021-01-01", "00:00:01")
		)
		self.assertIn(
			"""where "tabnote"."posting_date"+"tabnote"."posting_time">=cast('2021-01-01' as date)+cast('00:00:01' as time)""",
			str(select_query).lower(),
		)

		select_query = select_query.select(
			CombineDatetime(note.posting_date, note.posting_time, alias="timestamp")
		)
		self.assertIn(
			'"tabnote"."posting_date"+"tabnote"."posting_time" "timestamp"', str(select_query).lower()
		)

	def test_curdate(self):
		# CURRENT_DATE must render as a bare keyword (no parentheses); postgres rejects CURRENT_DATE().
		self.assertEqual("CURRENT_DATE", CurDate().get_sql())
		note = frappe.qb.DocType("Note")
		query = frappe.qb.from_(note).select(note.name).where(note.posting_date >= CurDate())
		self.assertIn("current_date", str(query).lower())
		self.assertNotIn("current_date(", str(query).lower())

	def test_month_quarter_postgres(self):
		# date_part(...) is double precision on postgres; it is wrapped in CAST(... AS INTEGER) so
		# MONTH/QUARTER match MySQL's integer result (no `2.0` leaking into report output).
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"cast(date_part('month',posting_date) as integer)",
			Month(note.posting_date).get_sql().lower(),
		)
		self.assertEqual(
			"cast(date_part('quarter',posting_date) as integer)",
			Quarter(note.posting_date).get_sql().lower(),
		)
		# round-trips to a python int, like MariaDB's MONTH()/QUARTER()
		val = frappe.db.sql(
			f"SELECT {Month(CurDate()).get_sql()} AS m, {Quarter(CurDate()).get_sql()} AS q",
			as_dict=True,
		)[0]
		self.assertIsInstance(val["m"], int)
		self.assertIsInstance(val["q"], int)

	def test_unix_ts_postgres(self):
		# Simple Query
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"cast(trunc(extract(epoch from (cast(posting_date as timestamp) "
			"at time zone current_setting('timezone')))) as bigint)",
			UnixTimestamp(note.posting_date).get_sql().lower(),
		)

		# Complex multi table query
		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(UnixTimestamp(note.posting_date))
		)
		self.assertIn(
			'cast(trunc(extract(epoch from (cast("tabnote"."posting_date" as timestamp) '
			"at time zone current_setting('timezone')))) as bigint)",
			str(select_query).lower(),
		)

		# Order by
		select_query = select_query.orderby(UnixTimestamp(note.posting_date))
		self.assertIn(
			'order by cast(trunc(extract(epoch from (cast("tabnote"."posting_date" as timestamp) '
			"at time zone current_setting('timezone')))) as bigint)",
			str(select_query).lower(),
		)

		# Function comparison
		select_query = select_query.where(
			UnixTimestamp(note.posting_date) >= UnixTimestamp(Date("2021-01-01"))
		)
		self.assertIn(
			'cast(trunc(extract(epoch from (cast("tabnote"."posting_date" as timestamp) '
			"at time zone current_setting('timezone')))) as bigint)"
			">=cast(trunc(extract(epoch from (cast(date('2021-01-01') as timestamp) "
			"at time zone current_setting('timezone')))) as bigint)",
			str(select_query).lower(),
		)

		# aliasing
		select_query = select_query.select(UnixTimestamp(note.posting_date, alias="unix_ts"))
		self.assertIn(
			'cast(trunc(extract(epoch from (cast("tabnote"."posting_date" as timestamp) '
			"at time zone current_setting('timezone')))) as bigint) \"unix_ts\"",
			str(select_query).lower(),
		)

	def test_unix_ts_postgres_truncates_fractional_seconds(self):
		# MariaDB's UNIX_TIMESTAMP carries the fraction; casting it to an int truncates. A bare
		# CAST(... AS BIGINT) rounds instead, pushing a .5+ timestamp a second into the future.
		dt = frappe.qb.DocType("DocType")
		for fraction, expected in ((".4", 0), (".5", 0), (".6", 0)):
			stamp = UnixTimestamp(Cast(f"2021-06-01 00:00:00{fraction}", "timestamp"))
			got = frappe.qb.from_(dt).select(stamp).limit(1).run()[0][0]
			baseline = (
				frappe.qb.from_(dt)
				.select(UnixTimestamp(Cast("2021-06-01 00:00:00", "timestamp")))
				.limit(1)
				.run()[0][0]
			)
			self.assertEqual(got - baseline, expected, msg=f"fraction {fraction}")

	def test_unix_ts_postgres_uses_session_timezone(self):
		from datetime import datetime
		from zoneinfo import ZoneInfo

		dt = frappe.qb.DocType("DocType")
		epoch = UnixTimestamp(Date("2021-06-01"))
		try:
			for tz in ("UTC", "Asia/Kolkata", "America/New_York"):
				frappe.db.sql("SET LOCAL TIME ZONE %s", (tz,))
				got = frappe.qb.from_(dt).select(epoch).limit(1).run()[0][0]
				expected = int(datetime(2021, 6, 1, tzinfo=ZoneInfo(tz)).timestamp())
				self.assertEqual(got, expected, msg=f"timezone {tz}")
		finally:
			frappe.db.sql("RESET TIME ZONE")

	def test_datediff_postgres(self):
		# Postgres subtracts dates to get an integer day count, matching MariaDB DATEDIFF.
		note = frappe.qb.DocType("Note")
		self.assertEqual(
			"CAST(posting_date AS DATE)-CAST(creation AS DATE)",
			DateDiff(note.posting_date, note.creation).get_sql(),
		)
		self.assertEqual(
			"CAST('2024-01-10' AS DATE)-CAST(creation AS DATE)",
			DateDiff("2024-01-10", note.creation).get_sql(),
		)

		todo = frappe.qb.DocType("ToDo")
		select_query = (
			frappe.qb.from_(note)
			.join(todo)
			.on(todo.refernce_name == note.name)
			.select(DateDiff(note.posting_date, note.creation))
		)
		self.assertIn(
			'select cast("tabnote"."posting_date" as date)-cast("tabnote"."creation" as date)',
			str(select_query).lower(),
		)

	def test_datediff_postgres_returns_whole_days_for_timestamps(self):
		# Subtracting two timestamps yields an interval that keeps the time of day, so a Datetime
		# operand would come back as a timedelta where MariaDB's DATEDIFF returns whole days.
		dt = frappe.qb.DocType("DocType")
		diff = DateDiff(Cast("2024-01-10 01:00:00", "timestamp"), Cast("2024-01-01 23:00:00", "timestamp"))
		got = frappe.qb.from_(dt).select(diff).limit(1).run()[0][0]
		self.assertEqual(got, 9)
		self.assertIsInstance(got, int)

	def test_time(self):
		note = frappe.qb.DocType("Note")

		self.assertEqual(
			"CAST('2021-01-01' AS DATE)+CAST('00:00:21' AS TIME)",
			CombineDatetime("2021-01-01", time(0, 0, 21)).get_sql(),
		)

		select_query = frappe.qb.from_(note).select(CombineDatetime(note.posting_date, note.posting_time))
		self.assertIn('select "posting_date"+"posting_time"', str(select_query).lower())

		select_query = select_query.where(
			CombineDatetime(note.posting_date, note.posting_time)
			>= CombineDatetime("2021-01-01", time(0, 0, 1))
		)
		self.assertIn(
			"""where "posting_date"+"posting_time">=cast('2021-01-01' as date)+cast('00:00:01' as time)""",
			str(select_query).lower(),
		)

	def test_cast(self):
		note = frappe.qb.DocType("Note")
		self.assertEqual("CAST(name AS VARCHAR)", Cast_(note.name, "varchar").get_sql())
		self.assertEqual("CAST(name AS INTEGER)", Cast_(note.name, "integer").get_sql())
		self.assertEqual(
			frappe.qb.from_("red").from_(note).select("other", Cast_(note.name, "varchar")).get_sql(),
			'SELECT "tabred"."other",CAST("tabNote"."name" AS VARCHAR) FROM "tabred","tabNote"',
		)

	def test_round(self):
		note = frappe.qb.DocType("Note")

		query = frappe.qb.from_(note).select(Round(note.price))
		self.assertEqual('select round("price",0) from "tabnote"', str(query).lower())

		query = frappe.qb.from_(note).select(Round(note.price, 3))
		self.assertEqual('select round("price",3) from "tabnote"', str(query).lower())

	def test_truncate(self):
		note = frappe.qb.DocType("Note")
		query = frappe.qb.from_(note).select(Truncate(note.price, 3))
		self.assertEqual('select truncate("price",3) from "tabnote"', str(query).lower())

	def test_json_extract(self):
		note = frappe.qb.DocType("Note")
		# Simple get_sql
		self.assertEqual("\"content\"->'$.key'", JSONExtract(note.content, "$.key").get_sql())

		# In a SELECT query
		query = frappe.qb.from_(note).select(JSONExtract(note.content, "$.key"))
		self.assertIn("\"content\"->'$.key'", str(query))

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONExtract(note.content, "$.key") == "value")
		self.assertIn("\"content\"->'$.key'='value'", str(query))

	def test_json_value(self):
		note = frappe.qb.DocType("Note")
		# Simple get_sql
		self.assertEqual("\"content\"->>'$.key'", JSONValue(note.content, "$.key").get_sql())

		# In a SELECT query
		query = frappe.qb.from_(note).select(JSONValue(note.content, "$.key"))
		self.assertIn("\"content\"->>'$.key'", str(query))

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONValue(note.content, "$.key") == "value")
		self.assertIn("\"content\"->>'$.key'='value'", str(query))

	def test_json_contains(self):
		note = frappe.qb.DocType("Note")
		# With a plain string candidate
		self.assertEqual("\"content\"@>'admin'", JSONContains(note.content, "admin").get_sql())

		# In a WHERE clause
		query = frappe.qb.from_(note).select(note.name).where(JSONContains(note.content, "admin"))
		self.assertIn("\"content\"@>'admin'", str(query))


class TestBuilderBase:
	def test_adding_tabs(self):
		self.assertEqual("tabNotes", frappe.qb.DocType("Notes").get_sql())
		self.assertEqual("__Auth", frappe.qb.DocType("__Auth").get_sql())
		self.assertEqual("Notes", frappe.qb.Table("Notes").get_sql())

	def test_run_patcher(self):
		query = frappe.qb.from_("ToDo").select("*").limit(1)
		data = query.run(as_dict=True)
		self.assertTrue("run" in dir(query))
		self.assertIsInstance(query.run, Callable)
		self.assertIsInstance(data, list)

	def test_agg_funcs(self):
		doc = new_doctype(
			fields=[
				{
					"fieldname": "number",
					"fieldtype": "Int",
					"label": "Number",
					"reqd": 1,  # mandatory
				},
			],
		)
		doc.insert()
		self.doctype_name = doc.name
		frappe.db.truncate(self.doctype_name)
		sample_data = {
			"doctype": self.doctype_name,
			"number": 1,
		}
		frappe.get_doc(sample_data).insert(ignore_mandatory=True)
		sample_data["number"] = 3
		frappe.get_doc(sample_data).insert(ignore_mandatory=True)
		sample_data["number"] = 4
		frappe.get_doc(sample_data).insert(ignore_mandatory=True)
		self.assertEqual(frappe.qb.max(self.doctype_name, "number"), 4)
		self.assertEqual(frappe.qb.min(self.doctype_name, "number"), 1)
		self.assertAlmostEqual(frappe.qb.avg(self.doctype_name, "number"), 2.666, places=2)
		self.assertEqual(frappe.qb.sum(self.doctype_name, "number"), 8.0)
		frappe.db.rollback()


class TestParameterization(IntegrationTestCase):
	def test_where_conditions(self):
		DocType = frappe.qb.DocType("DocType")
		query = frappe.qb.from_(DocType).select(DocType.name).where(DocType.owner == "Administrator' --")
		self.assertTrue("walk" in dir(query))
		query, params = query.walk()

		self.assertIn("%(param1)s", query)
		self.assertIn("param1", params)
		self.assertEqual(params["param1"], "Administrator' --")

	def test_set_conditions(self):
		DocType = frappe.qb.DocType("DocType")
		query = frappe.qb.update(DocType).set(DocType.value, "some_value")

		self.assertTrue("walk" in dir(query))
		query, params = query.walk()

		self.assertIn("%(param1)s", query)
		self.assertIn("param1", params)
		self.assertEqual(params["param1"], "some_value")

	def test_bool_conditions(self):
		# bools go out as '1'/'0': quoted, so they work as a value and as a condition
		DocType = frappe.qb.DocType("DocType")
		query, params = frappe.qb.update(DocType).set(DocType.is_submittable, True).walk()

		self.assertIn("='1'", query)
		self.assertNotIn("true", query)
		self.assertEqual(params, {})

		query, _ = frappe.qb.update(DocType).set(DocType.is_submittable, False).walk()
		self.assertIn("='0'", query)

		# a bool as a condition, not as a value: it must stay a quoted literal, since
		# postgres accepts neither a bare 1 nor `true` as an operand of OR
		user = frappe.qb.DocType("User")
		condition = frappe.qb.from_(user).select(user.name).where((user.enabled == 1) | ValueWrapper(False))

		query, _ = condition.walk()
		self.assertIn("OR '0'", query)
		self.assertNotIn("OR 0", query)
		self.assertNotIn("OR false", query)
		condition.run()  # and the database accepts it

	def test_where_conditions_functions(self):
		DocType = frappe.qb.DocType("DocType")
		query = (
			frappe.qb.from_(DocType).select(DocType.name).where(Coalesce(DocType.search_fields == "subject"))
		)

		self.assertTrue("walk" in dir(query))
		query, params = query.walk()

		self.assertIn("%(param1)s", query)
		self.assertIn("param1", params)
		self.assertEqual(params["param1"], "subject")

	def test_case(self):
		DocType = frappe.qb.DocType("DocType")
		query = frappe.qb.from_(DocType).select(
			Case()
			.when(DocType.search_fields == "value", "other_value")
			.when(Coalesce(DocType.search_fields == "subject_in_function"), "true_value")
			.else_("Overdue")
		)

		self.assertTrue("walk" in dir(query))
		query, params = query.walk()

		self.assertIn("%(param1)s", query)
		self.assertIn("param1", params)
		self.assertEqual(params["param1"], "value")
		self.assertEqual(params["param2"], "other_value")
		self.assertEqual(params["param3"], "subject_in_function")
		self.assertEqual(params["param4"], "true_value")
		self.assertEqual(params["param5"], "Overdue")

	def test_case_in_update(self):
		DocType = frappe.qb.DocType("DocType")
		query = frappe.qb.update(DocType).set(
			"parent",
			Case()
			.when(DocType.search_fields == "value", "other_value")
			.when(Coalesce(DocType.search_fields == "subject_in_function"), "true_value")
			.else_("Overdue"),
		)

		self.assertTrue("walk" in dir(query))
		query, params = query.walk()

		self.assertIn("%(param1)s", query)
		self.assertIn("param1", params)
		self.assertEqual(params["param1"], "value")
		self.assertEqual(params["param2"], "other_value")
		self.assertEqual(params["param3"], "subject_in_function")
		self.assertEqual(params["param4"], "true_value")
		self.assertEqual(params["param5"], "Overdue")

	def test_named_parameter_wrapper(self):
		from frappe.query_builder.terms import NamedParameterWrapper

		test_npw = NamedParameterWrapper()
		self.assertTrue(hasattr(test_npw, "parameters"))
		self.assertEqual(test_npw.get_sql("test_string_one"), "%(param1)s")
		self.assertEqual(test_npw.get_sql("test_string_two"), "%(param2)s")
		params = test_npw.get_parameters()
		for key in params.keys():
			# checks for param# format
			self.assertRegex(key, r"param\d")
		self.assertEqual(params["param1"], "test_string_one")


@run_only_if(db_type_is.MARIADB)
class TestBuilderMaria(IntegrationTestCase, TestBuilderBase):
	def test_adding_tabs_in_from(self):
		self.assertEqual("SELECT * FROM `tabNotes`", frappe.qb.from_("Notes").select("*").get_sql())
		self.assertEqual("SELECT * FROM `__Auth`", frappe.qb.from_("__Auth").select("*").get_sql())

	def test_get_qb_type(self):
		from frappe.query_builder import get_query_builder

		qb = get_query_builder(frappe.db.db_type)
		self.assertEqual("SELECT * FROM `tabDocType`", qb().from_("DocType").select("*").get_sql())


@run_only_if(db_type_is.POSTGRES)
class TestBuilderPostgres(IntegrationTestCase, TestBuilderBase):
	def test_adding_tabs_in_from(self):
		self.assertEqual('SELECT * FROM "tabNotes"', frappe.qb.from_("Notes").select("*").get_sql())
		self.assertEqual('SELECT * FROM "__Auth"', frappe.qb.from_("__Auth").select("*").get_sql())

	def test_replace_tables(self):
		info_schema = frappe.qb.Schema("information_schema")
		self.assertEqual(
			'SELECT * FROM "pg_stat_all_tables"',
			frappe.qb.from_(info_schema.tables).select("*").get_sql(),
		)

	def test_replace_fields_post(self):
		self.assertEqual("relname", frappe.qb.Field("table_name").get_sql())

	def test_get_qb_type(self):
		from frappe.query_builder import get_query_builder

		qb = get_query_builder(frappe.db.db_type)
		self.assertEqual('SELECT * FROM "tabDocType"', qb().from_("DocType").select("*").get_sql())


class TestMisc(IntegrationTestCase):
	def test_custom_func(self):
		rand_func = frappe.qb.functions("rand", "45")
		self.assertIsInstance(rand_func, Function)
		self.assertEqual(rand_func.get_sql(), "rand('45')")

	def test_function_with_schema(self):
		from frappe.query_builder import ParameterizedFunction

		x = ParameterizedFunction("rand", "45")
		x.schema = frappe.qb.DocType("DocType")
		self.assertEqual("tabDocType.rand('45')", x.get_sql())

	def test_util_table(self):
		from frappe.query_builder.utils import Table

		DocType = Table("DocType")
		self.assertEqual(DocType.get_sql(), "DocType")

	def test_union(self):
		user = frappe.qb.DocType("User")
		role = frappe.qb.DocType("Role")
		users = frappe.qb.from_(user).select(user.name)
		roles = frappe.qb.from_(role).select(role.name)

		self.assertEqual(set(users.run() + roles.run()), set((users + roles).run()))


class TestOperatorIn(IntegrationTestCase):
	def test_func_in_without_empty_values(self):
		note = frappe.qb.DocType("Note")
		query = func_in(note.name, ["n1", "n2", "n3"])
		sql_str = str(query).lower()

		self.assertIn("in", sql_str)
		self.assertNotIn("coalesce", sql_str)

	def test_func_in_with_none_converts_to_empty_string(self):
		note = frappe.qb.DocType("Note")
		query = func_in(note.name, [None, "user1"])
		sql_str = str(query).lower()

		self.assertNotIn("coalesce", sql_str)
		self.assertIn("is null", sql_str)
		self.assertIn("''", sql_str)

	def test_func_in_with_empty_string_uses_or_is_null(self):
		note = frappe.qb.DocType("Note")
		query = func_in(note.name, ["", "user1"])
		sql_str = str(query).lower()

		self.assertNotIn("coalesce", sql_str)
		self.assertIn("is null", sql_str)
		self.assertIn("''", sql_str)

	def test_func_in_with_mixed_none_and_values(self):
		note = frappe.qb.DocType("Note")
		query = func_in(note.name, ["val1", None, "val2"])
		sql_str = str(query).lower()

		self.assertNotIn("coalesce", sql_str)
		self.assertIn("is null", sql_str)

	def test_in_filter_matches_null_and_empty_columns(self):
		test_doctype = new_doctype(
			fields=[
				{
					"fieldname": "test_field",
					"fieldtype": "Data",
					"label": "Test Field",
				},
			],
		)
		test_doctype.insert()
		self.test_doctype_name = test_doctype.name
		self.addCleanup(frappe.delete_doc, "DocType", self.test_doctype_name)

		doc_null = frappe.get_doc({"doctype": self.test_doctype_name, "test_field": None})
		doc_null.insert()
		doc_empty = frappe.get_doc({"doctype": self.test_doctype_name, "test_field": ""})
		doc_empty.insert()
		doc_user = frappe.get_doc({"doctype": self.test_doctype_name, "test_field": "user1"})
		doc_user.insert()

		results = frappe.get_all(
			self.test_doctype_name,
			filters={"test_field": ["in", [None, "user1"]]},
			pluck="test_field",
		)

		self.assertIn(None, results)
		self.assertIn("", results)
		self.assertIn("user1", results)


class TestRecursiveCTE(IntegrationTestCase):
	def test_recursive_keyword_is_emitted(self):
		# recursive=True renders WITH RECURSIVE so a CTE may reference itself in its recursive term.
		from pypika import AliasedQuery, Table

		nodes = Table("nodes")
		tree = AliasedQuery("tree")
		seed = frappe.qb.from_(nodes).select(nodes.name, nodes.parent).where(nodes.parent.isnull())
		recurse = (
			frappe.qb.from_(nodes).join(tree).on(nodes.parent == tree.name).select(nodes.name, nodes.parent)
		)
		query = frappe.qb.with_(seed + recurse, "tree", recursive=True).from_(tree).select(tree.name)
		self.assertIn("WITH RECURSIVE tree AS", query.get_sql())

	def test_non_recursive_with_matches_pypika(self):
		# recursive defaults to False and must keep pypika's exact multi-CTE formatting (the join
		# is ") ," between clauses) -- the override changes nothing on the non-recursive path.
		from pypika import AliasedQuery, Table

		a = frappe.qb.from_(Table("t1")).select("x")
		b = frappe.qb.from_(Table("t2")).select("y")
		sql = frappe.qb.with_(a, "cte_a").with_(b, "cte_b").from_(AliasedQuery("cte_a")).select("x").get_sql()
		self.assertTrue(sql.startswith("WITH cte_a AS "))
		self.assertNotIn("WITH RECURSIVE", sql)
		self.assertIn(") ,cte_b AS (", sql)


class TestPerDbType(IntegrationTestCase):
	"""The render-time dispatch every backend-sensitive term is built on (`per_db_type`)."""

	def test_dispatch_follows_the_rendering_dialect_not_the_site(self):
		# one object, spelled per database at render time: the site's db_type is not consulted
		term = GroupConcat("Notes")
		self.assertEqual("GROUP_CONCAT('Notes' SEPARATOR ',')", term.get_sql(dialect=Dialects.MYSQL))
		self.assertEqual("STRING_AGG('Notes',',')", term.get_sql(dialect=Dialects.POSTGRESQL))

	@unimplemented_for(db_type_is.SQLITE)
	def test_bare_render_defaults_to_the_site_database(self):
		# a term rendered outside a query has no dialect in scope and falls back to frappe.qb's
		expected = {"mariadb": "GROUP_CONCAT('Notes' SEPARATOR ',')", "postgres": "STRING_AGG('Notes',',')"}
		self.assertEqual(expected[frappe.conf.db_type], GroupConcat("Notes").get_sql())

	def test_undeclared_database_raises_instead_of_rendering_the_wrong_spelling(self):
		with self.assertRaises(UnsupportedOperation) as raised:
			GroupConcat("Notes").get_sql(dialect=Dialects.SQLLITE)
		self.assertIn("GroupConcat.as_sqlite()", str(raised.exception))

	def test_default_for(self):
		# bare: the default is BASE_DB_TYPE's spelling, every other database must be declared
		@per_db_type
		class Base(Function):
			def __init__(self):
				super().__init__("F", 1)

		# a set: the default is right on exactly these
		@per_db_type(default_for={"mariadb", "sqlite"})
		class Two(Function):
			def __init__(self):
				super().__init__("F", 1)

			def as_postgres(self, **kwargs):
				return "G(1)"

		# "*": the default is right everywhere, methods are exceptions
		@per_db_type(default_for="*")
		class Universal(Function):
			def __init__(self):
				super().__init__("F", 1)

		self.assertEqual("F(1)", Base().get_sql(dialect=Dialects.MYSQL))
		self.assertRaises(UnsupportedOperation, Base().get_sql, dialect=Dialects.POSTGRESQL)
		self.assertRaises(UnsupportedOperation, Base().get_sql, dialect=Dialects.SQLLITE)
		self.assertEqual("F(1)", Two().get_sql(dialect=Dialects.MYSQL))
		self.assertEqual("F(1)", Two().get_sql(dialect=Dialects.SQLLITE))
		self.assertEqual("G(1)", Two().get_sql(dialect=Dialects.POSTGRESQL))
		for dialect in (Dialects.MYSQL, Dialects.POSTGRESQL, Dialects.SQLLITE):
			self.assertEqual("F(1)", Universal().get_sql(dialect=dialect))

	def test_cast_is_universal_with_mariadb_as_the_exception(self):
		self.assertEqual("CAST('5' AS VARCHAR)", Cast_("5", "varchar").get_sql(dialect=Dialects.SQLLITE))
		self.assertEqual("CAST('5' AS VARCHAR)", Cast_("5", "varchar").get_sql(dialect=Dialects.POSTGRESQL))
		self.assertEqual("CONCAT('5','')", Cast_("5", "varchar").get_sql(dialect=Dialects.MYSQL))
		# only a varchar cast is special on mariadb
		self.assertEqual("CAST('5' AS INTEGER)", Cast_("5", "integer").get_sql(dialect=Dialects.MYSQL))

	def test_raw_args_keeps_the_operands_pypika_wraps(self):
		# _PostgresTimestamp casts a `str` operand; after Function.__init__ it is a ValueWrapper and
		# the isinstance check can never fire -- raw_args is what lets the CASTs survive
		term = CombineDatetime("2021-01-01", "00:00:21")
		self.assertEqual(("2021-01-01", "00:00:21"), term.raw_args)
		self.assertEqual(
			"CAST('2021-01-01' AS DATE)+CAST('00:00:21' AS TIME)", term.get_sql(dialect=Dialects.POSTGRESQL)
		)

	def test_undecorated_term_is_untouched(self):
		term = Round("1.234", 2)
		self.assertFalse(hasattr(term, "raw_args"))
		for dialect in (Dialects.MYSQL, Dialects.POSTGRESQL, Dialects.SQLLITE):
			self.assertEqual("ROUND('1.234',2)", term.get_sql(dialect=dialect))

	def test_values_stay_parameterised_through_every_spelling(self):
		from frappe.query_builder.terms import NamedParameterWrapper

		hostile = "a' OR 1=1 --"
		note = frappe.qb.DocType("Note")
		query = frappe.qb.from_(note).select(
			DateFormat(note.creation, hostile), Locate(hostile, note.title), CombineDatetime(hostile, hostile)
		)
		for dialect in (Dialects.MYSQL, Dialects.POSTGRESQL):
			params = NamedParameterWrapper()
			sql = query.get_sql(param_wrapper=params, dialect=dialect)
			self.assertNotIn(hostile, sql)
			self.assertEqual(4, len(params.parameters))


class TestDbTypeMatrix(IntegrationTestCase):
	"""Every backend-sensitive term, rendered for every database and executed where it can be.

	Execution runs on the site's own database and on SQLite in-process; the remaining databases
	are render-only here and execute when the suite runs on their site. Red cells are declared:
	UNSUPPORTED names the terms that refuse a database, KNOWN_INVALID the ones that render but the
	database rejects. Both are asserted in both directions, so fixing a hole or opening one fails
	this test until the table says so.
	"""

	# rendered over literals so a cell executes without a table; the FIELD_ONLY ones need a column
	# (a fulltext index, a JSON column, a real identifier) and are render-only everywhere
	DATE = Cast("2021-01-05", "date")
	FIELD_ONLY = frozenset(
		{
			"Match",
			"JSONExtract",
			"JSONValue",
			"JSONContains",
			"Like",
			"NotLike",
			"Regex",
			"PseudoColumn",
			"NameAsText",
			"IsSet",
			"IsNotSet",
			"TextLike",
		}
	)
	UNSUPPORTED = frozenset(
		("sqlite", name)
		for name in (
			"GroupConcat",
			"Match",
			"CombineDatetime",
			"DateFormat",
			"UnixTimestamp",
			"DateDiff",
			"JSONExtract",
			"JSONValue",
			"JSONContains",
			"MonthName",
			"Quarter",
			"Month",
			"Year",
		)
	)
	# undecorated terms whose default is not, in fact, universal -- the debt the matrix records
	KNOWN_INVALID = frozenset(
		(db, name) for db in ("postgres", "sqlite") for name in ("Truncate", "Timestamp", "YearWeek")
	)

	@classmethod
	def cases(cls):
		doctype = frappe.qb.DocType("DocType")
		column = frappe.qb.DocType("x").c
		return {
			"Locate": Locate("b", "abc"),
			"GroupConcat": GroupConcat("Notes").distinct().separator(" | "),
			"Match": Match(doctype.name).Against("x"),
			"CombineDatetime": CombineDatetime("2021-01-05", "00:00:21"),
			"DateFormat": DateFormat(cls.DATE, "%Y-%m"),
			"UnixTimestamp": UnixTimestamp(cls.DATE),
			"DateDiff": DateDiff(cls.DATE, cls.DATE),
			"JSONExtract": JSONExtract(doctype.name, "$.a"),
			"JSONValue": JSONValue(doctype.name, "$.a"),
			"JSONContains": JSONContains(doctype.name, "x"),
			"MonthName": MonthName(cls.DATE),
			"Quarter": Quarter(cls.DATE),
			"Month": Month(cls.DATE),
			"Year": Year(cls.DATE),
			"Cast_": Cast_("5", "varchar"),
			"Like": column.like("a%"),
			"NotLike": column.not_like("a%"),
			"Regex": column.regex("^a"),
			"PseudoColumn": PseudoColumnMapper("`tabDocType`.`name`"),
			"NameAsText": NameAsText(doctype.name),
			"IsSet": IsSet(doctype.idx, "DocType", "idx"),
			"IsNotSet": IsNotSet(doctype.modified, "DocType", "modified"),
			"TextLike": TextLike(doctype.idx, "1%", OPERATOR_MAP["like"]),
			"Round": Round("1.234", 2),
			"Truncate": Truncate("1.234", 2),
			"Timestamp": Timestamp("2021-01-05"),
			"YearWeek": YearWeek(cls.DATE),
		}

	def render(self, term, db_type):
		spec = DB_TYPES[db_type_is(db_type)]
		return term.get_sql(dialect=spec.pypika, quote_char=spec.builder._BuilderClasss.QUOTE_CHAR)

	def test_every_cell_renders_or_is_declared_unsupported(self):
		for db_type in DB_TYPES:
			for name, term in self.cases().items():
				with self.subTest(db_type=db_type.value, term=name):
					if (db_type.value, name) in self.UNSUPPORTED:
						self.assertRaises(UnsupportedOperation, self.render, term, db_type.value)
					else:
						self.assertTrue(self.render(term, db_type.value))

	def test_every_renderable_cell_executes_on_the_site_database(self):
		db_type = frappe.conf.db_type
		for name, term in self.cases().items():
			if name in self.FIELD_ONLY or (db_type, name) in self.UNSUPPORTED:
				continue
			with self.subTest(term=name):
				sql = f"select {self.render(term, db_type)}"
				if (db_type, name) in self.KNOWN_INVALID:
					with self.assertRaises(Exception):
						frappe.db.sql(sql)
					frappe.db.rollback()
				else:
					frappe.db.sql(sql)

	def test_every_renderable_cell_executes_on_sqlite(self):
		# SQLite runs in-process, so its column of the matrix is proven on every site
		import sqlite3

		connection = sqlite3.connect(":memory:")
		for name, term in self.cases().items():
			if name in self.FIELD_ONLY or ("sqlite", name) in self.UNSUPPORTED:
				continue
			with self.subTest(term=name):
				sql = f"select {self.render(term, 'sqlite')}"
				if ("sqlite", name) in self.KNOWN_INVALID:
					self.assertRaises(sqlite3.OperationalError, connection.execute, sql)
				else:
					connection.execute(sql).fetchone()


class TestCompileQuery(IntegrationTestCase):
	"""`compile_query` shapes a tree for the database it renders for, and only for that one."""

	def setUp(self):
		self.note = frappe.qb.DocType("Note")

	def render(self, query, db_type):
		spec = DB_TYPES[db_type_is(db_type)]
		return query.get_sql(dialect=spec.pypika, quote_char=spec.builder._BuilderClasss.QUOTE_CHAR)

	def test_offset_gets_a_limit_only_where_required(self):
		query = frappe.qb.from_(self.note).select(self.note.name).offset(5)
		self.assertIn(f"LIMIT {MAX_LIMIT} OFFSET 5", self.render(query, "mariadb"))
		self.assertIn(f"LIMIT {MAX_LIMIT} OFFSET 5", self.render(query, "sqlite"))
		self.assertNotIn("LIMIT", self.render(query, "postgres"))
		# an explicit limit is never replaced
		query = frappe.qb.from_(self.note).select(self.note.name).limit(3).offset(5)
		self.assertIn("LIMIT 3 OFFSET 5", self.render(query, "mariadb"))

	def test_index_hints_are_dropped_where_unsupported(self):
		query = frappe.qb.from_(self.note).select(self.note.name).force_index("idx_title")
		self.assertIn("FORCE INDEX", self.render(query, "mariadb"))
		self.assertNotIn("FORCE INDEX", self.render(query, "postgres"))
		self.assertNotIn("FORCE INDEX", self.render(query, "sqlite"))

	def test_distinct_drops_unselected_order_by_only_where_required_and_warns(self):
		query = frappe.qb.from_(self.note).select(self.note.title).distinct().orderby(self.note.modified)
		query._shape = QueryShape(order_by_unselected=True)  # what Engine records
		self.assertIn("ORDER BY", self.render(query, "mariadb"))
		with self.assertWarnsRegex(UserWarning, "ORDER BY fields have been ignored"):
			self.assertNotIn("ORDER BY", self.render(query, "postgres"))
		# without the annotation nothing is dropped anywhere
		query._shape = QueryShape()
		self.assertIn("ORDER BY", self.render(query, "postgres"))

	def test_strict_group_by_applies_engine_annotations(self):
		user = frappe.qb.DocType("User")
		ordered = self.note.modified
		query = (
			frappe.qb.from_(self.note)
			.left_join(user)
			.on(user.name == self.note.owner)
			.select(self.note.name, user.full_name)
			.groupby(self.note.name)
			.orderby(ordered)
		)
		query._shape = QueryShape(
			group_by_extension=(user.name,), aggregate_order_terms=frozenset({id(ordered)})
		)
		mariadb, postgres = self.render(query, "mariadb"), self.render(query, "postgres")
		self.assertIn("GROUP BY `tabNote`.`name` ORDER BY `tabNote`.`modified`", mariadb)
		self.assertIn(
			'GROUP BY "tabNote"."name","tabUser"."name" ORDER BY MAX("tabNote"."modified")', postgres
		)

	def test_compile_never_mutates_the_query(self):
		query = (
			frappe.qb.from_(self.note)
			.select(self.note.title)
			.distinct()
			.orderby(self.note.modified)
			.offset(2)
		)
		query._shape = QueryShape(order_by_unselected=True)
		before = (list(query._orderbys), query._limit, list(query._groupbys))
		with self.assertWarns(UserWarning):
			self.render(query, "postgres")
		self.render(query, "mariadb")
		self.assertEqual(before, (list(query._orderbys), query._limit, list(query._groupbys)))
		# and a second render for the same database gives the same text
		self.assertEqual(self.render(query, "mariadb"), self.render(query, "mariadb"))

	def test_unknown_dialect_raises_instead_of_rendering_unshaped(self):
		query = frappe.qb.from_(self.note).select(self.note.name).offset(5)
		with self.assertRaises(UnsupportedOperation) as raised:
			compile_query(query, dialect="clickhouse")
		self.assertIn("add a DbType row", str(raised.exception))

	def test_each_rule_is_a_no_op_without_its_flag(self):
		# a DbType with every flag off (index hints allowed, so that rule has nothing to strip):
		# no rule may touch the query, whatever Engine recorded on it
		bare = DbType("bare", frappe.qb, Dialects.MYSQL, supports_index_hints=True)
		user = frappe.qb.DocType("User")
		ordered = self.note.modified
		query = (
			frappe.qb.from_(self.note)
			.left_join(user)
			.on(user.name == self.note.owner)
			.select(self.note.title)
			.distinct()
			.groupby(self.note.name)
			.orderby(ordered)
			.offset(2)
			.force_index("idx")
		)
		query._shape = QueryShape(
			order_by_unselected=True,
			group_by_extension=(user.name,),
			aggregate_order_terms=frozenset({id(ordered)}),
		)
		for rule in SHAPE_RULES:
			with self.subTest(rule=rule.__name__):
				self.assertIs(rule(query, bare), query)  # untouched, not even copied

	def test_default_shape_changes_nothing_on_a_strict_database(self):
		query = frappe.qb.from_(self.note).select(self.note.title).distinct().orderby(self.note.modified)
		query._shape = QueryShape()
		self.assertIn("ORDER BY", self.render(query, "postgres"))


class TestEngineIsDatabaseNeutral(IntegrationTestCase):
	"""One Engine-built tree renders correctly for every database: no database is consulted
	while building, only while rendering."""

	def render(self, query, db_type):
		spec = DB_TYPES[db_type_is(db_type)]
		return query.get_sql(dialect=spec.pypika, quote_char=spec.builder._BuilderClasss.QUOTE_CHAR)

	def test_same_tree_two_databases(self):
		query = frappe.qb.get_query(
			"ToDo", fields=["name"], filters={"idx": ["is", "set"], "description": ["like", "%x%"]}, offset=5
		)
		self.assertEqual(
			f"SELECT `name` FROM `tabToDo` WHERE `idx`<>'' AND `description` LIKE '%x%' LIMIT {MAX_LIMIT} OFFSET 5",
			self.render(query, "mariadb"),
		)
		self.assertEqual(
			"""SELECT "name" FROM "tabToDo" WHERE "idx"<>0 AND "description" ILIKE '%x%' OFFSET 5""",
			self.render(query, "postgres"),
		)

	def test_is_set_on_a_date_column(self):
		query = frappe.qb.get_query("ToDo", fields=["name"], filters={"date": ["is", "not set"]})
		self.assertIn("`date` IS NULL OR `date`=''", self.render(query, "mariadb"))
		self.assertIn(""""date" IS NULL OR "date"='0001-01-01'""", self.render(query, "postgres"))

	def test_like_on_a_non_text_column(self):
		query = frappe.qb.get_query("ToDo", fields=["name"], filters={"idx": ["like", "1%"]})
		self.assertIn("`idx` LIKE '1%'", self.render(query, "mariadb"))
		self.assertIn("""CAST("idx" AS VARCHAR) ILIKE '1%'""", self.render(query, "postgres"))

	def test_aggregate_order_by_under_strict_group_by(self):
		query = frappe.qb.get_query(
			"ToDo",
			fields=["status", {"COUNT": "name", "as": "n"}],
			group_by="status",
			order_by="modified desc",
		)
		self.assertIn("ORDER BY `modified` DESC", self.render(query, "mariadb"))
		self.assertIn('ORDER BY MAX("modified") DESC', self.render(query, "postgres"))

	def test_postgres_modify_query_leaves_query_builder_output_alone(self):
		# raw frappe.db.sql strings written in MariaDB spelling are rewritten textually for
		# postgres; query-builder output is already postgres and must pass through untouched
		from frappe.database.postgres.database import modify_query

		for query in (
			frappe.qb.get_query(
				"ToDo", fields=["name", "allocated_to.full_name"], filters={"idx": ["like", "1%"]}
			),
			frappe.qb.get_query("ToDo", fields=["name"], filters={"date": ["is", "not set"]}, offset=5),
			frappe.qb.get_query(
				"User", fields=["name", "roles.role"], filters={"roles.role": "System Manager"}
			),
		):
			sql = self.render(query, "postgres")
			self.assertEqual(sql, modify_query(sql))
