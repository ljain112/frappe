from datetime import time
from enum import Enum

from pypika.functions import *
from pypika.terms import Arithmetic, ArithmeticExpression, CustomFunction, Function, Term
from pypika.utils import format_alias_sql

import frappe
from frappe.query_builder.custom import (
	GROUP_CONCAT,
	MATCH,
	STRING_AGG,
	TO_TSVECTOR,
	Month,
	MonthName,
	Quarter,
	Year,
)
from frappe.query_builder.utils import DialectTerm, ImportMapper, db_type_is

from .utils import PseudoColumn


class Concat_ws(Function):
	def __init__(self, *terms, **kwargs):
		super().__init__("CONCAT_WS", *terms, **kwargs)


class Locate(Function):
	def __init__(self, needle, haystack, **kwargs):
		super().__init__("LOCATE", needle, haystack, **kwargs)


class Strpos(Function):
	def __init__(self, needle, haystack, **kwargs):
		super().__init__("STRPOS", haystack, needle, **kwargs)


class Instr(Function):
	def __init__(self, needle, haystack, **kwargs):
		super().__init__("INSTR", haystack, needle, **kwargs)


class Locate(DialectTerm, Locate):
	"""LOCATE(needle, haystack), in each backend's own spelling."""

	def as_postgres(self, **kwargs):
		return Strpos(*self.args, alias=self.alias).get_sql(**kwargs)

	# DuckDB has no LOCATE; strpos is the postgres spelling and works unchanged
	as_duckdb = as_postgres

	def as_sqlite(self, **kwargs):
		return Instr(*self.args, alias=self.alias).get_sql(**kwargs)


# for backward compatibility
Ifnull = IfNull


class Timestamp(Function):
	def __init__(self, term: str, time=None, alias=None):
		if time:
			super().__init__("TIMESTAMP", term, time, alias=alias)
		else:
			super().__init__("TIMESTAMP", term, alias=alias)


class Round(Function):
	def __init__(self, term, decimal=0, **kwargs):
		super().__init__("ROUND", term, decimal, **kwargs)


class Truncate(Function):
	def __init__(self, term, decimal, **kwargs):
		super().__init__("TRUNCATE", term, decimal, **kwargs)


class Abs(Function):
	# pypika ships Abs as an AggregateFunction, which makes get_list/get_value treat a scalar
	# ABS(...) select field as an aggregate query. On postgres that forces the default ORDER BY
	# to be wrapped in MAX(), turning the statement into an implicit aggregate and breaking the
	# (non-grouped) ABS column. ABS is scalar, so define it as a plain Function.
	def __init__(self, term, alias=None):
		super().__init__("ABS", term, alias=alias)


class CurDate(Term):
	"""SQL standard ``CURRENT_DATE`` keyword.

	pypika ships CurDate as a Function, so it renders ``CURRENT_DATE()``. Postgres rejects the
	parentheses — CURRENT_DATE is a reserved keyword there, not a function — while MariaDB accepts
	the bare keyword too. Render it without parentheses so the same query builder works on both.
	"""

	def __init__(self, alias=None):
		super().__init__(alias=alias)

	def get_sql(self, **kwargs):
		with_alias = kwargs.pop("with_alias", False)
		if with_alias:
			return format_alias_sql("CURRENT_DATE", self.alias, **kwargs)
		return "CURRENT_DATE"


class GroupConcat(DialectTerm, GROUP_CONCAT):
	"""GROUP_CONCAT, rendered as STRING_AGG where that is the spelling.

	MySQL puts the delimiter in a SEPARATOR clause and postgres takes it as a second argument,
	so the separator travels on the term and each rendering places it itself.
	"""

	def as_postgres(self, **kwargs):
		term = STRING_AGG(self.args[0], self._separator, alias=self.alias)
		# `.distinct()` is a @builder method, so its state lives on the term this replaces and has
		# to travel with it -- dropping it silently widens the result instead of failing
		term._distinct = self._distinct
		return term.get_sql(**kwargs)

	# SEPARATOR is MySQL syntax; DuckDB takes the postgres form
	as_duckdb = as_postgres


class Match(DialectTerm, MATCH):
	"""Full-text search, which not every backend has.

	Frappe does not implement sqlite search through the query builder either -- see
	`frappe.search.sqlite_search.SQLiteSearch`, which maintains a dedicated FTS5 side-car index.
	DuckDB is in the same position: it has neither MATCH ... AGAINST nor to_tsvector, so this
	refuses rather than returning wrong rows.
	"""

	def as_postgres(self, **kwargs):
		return TO_TSVECTOR(self.args[0], alias=self.alias).Against(self._Against).get_sql(**kwargs)

	def as_duckdb(self, **kwargs):
		from frappe import _, throw

		throw(
			_("Full-text search is not available on a snapshot report."),
			title=_("Unsupported Query"),
		)


class _PostgresTimestamp(ArithmeticExpression):
	def __init__(self, datepart, timepart, alias=None):
		"""Postgres would need both datepart and timepart to be a string for concatenation"""
		if isinstance(timepart, time) or isinstance(datepart, time):
			timepart, datepart = str(timepart), str(datepart)
		if isinstance(datepart, str):
			datepart = Cast(datepart, "date")
		if isinstance(timepart, str):
			timepart = Cast(timepart, "time")

		super().__init__(operator=Arithmetic.add, left=datepart, right=timepart, alias=alias)


class CombineDatetime(DialectTerm, Function):
	"""TIMESTAMP(date, time); postgres and DuckDB add the two parts instead."""

	def __init__(self, date, time, alias=None):
		super().__init__("TIMESTAMP", date, time, alias=alias)

	def as_postgres(self, **kwargs):
		return _PostgresTimestamp(*self.args, alias=self.alias).get_sql(**kwargs)

	# TIMESTAMP(date, time) is a parser error on DuckDB; date + time is not
	as_duckdb = as_postgres


class DateFormat(DialectTerm, Function):
	"""DATE_FORMAT(date, format), in each backend's own spelling."""

	def __init__(self, date, format, alias=None):
		super().__init__("DATE_FORMAT", date, format, alias=alias)

	def as_postgres(self, **kwargs):
		return ToChar(*self.args, alias=self.alias).get_sql(**kwargs)

	def as_duckdb(self, **kwargs):
		# DuckDB has neither to_char nor date_format; strftime takes the same % codes as mariadb
		return Function("strftime", *self.args, alias=self.alias).get_sql(**kwargs)


class YearWeek(Function):
	def __init__(self, term):
		super().__init__("YEARWEEK", term, 1)


class _PostgresUnixTimestamp(Extract):
	# Note: this is just a special case of "Extract" function with "epoch" hardcoded.
	# Check super definition to see how it works.
	def __init__(self, field, alias=None):
		super().__init__("epoch", field=field, alias=alias)
		self.field = field

	def get_sql(self, **kwargs):
		with_alias = kwargs.pop("with_alias", False)
		field = self.field if isinstance(self.field, Term) else Term.wrap_constant(self.field)
		field_sql = field.get_sql(**kwargs)
		sql = (
			"CAST(TRUNC(EXTRACT(EPOCH FROM "
			f"(CAST({field_sql} AS TIMESTAMP) AT TIME ZONE CURRENT_SETTING('TimeZone')))) AS BIGINT)"
		)
		if with_alias:
			return format_alias_sql(sql, self.alias, **kwargs)
		return sql


class UnixTimestamp(DialectTerm, Function):
	"""unix_timestamp(date); elsewhere an epoch extraction."""

	def __init__(self, date, alias=None):
		super().__init__("unix_timestamp", date, alias=alias)

	def as_postgres(self, **kwargs):
		return _PostgresUnixTimestamp(*self.args, alias=self.alias).get_sql(**kwargs)

	# DuckDB has no unix_timestamp either, and the postgres epoch expression works there
	as_duckdb = as_postgres


class _PostgresDateDiff(ArithmeticExpression):
	"""Postgres subtracts two dates to get an integer number of days, which matches
	MariaDB's DATEDIFF(date1, date2). Both operands are cast to date: subtracting
	timestamps would yield an interval that carries the time of day, so a Datetime
	field would return a timedelta where MariaDB returns whole days."""

	def __init__(self, date1, date2, alias=None):
		super().__init__(
			operator=Arithmetic.sub,
			left=Cast(date1, "date"),
			right=Cast(date2, "date"),
			alias=alias,
		)


class DateDiff(DialectTerm, Function):
	"""DATEDIFF(date1, date2); postgres and DuckDB subtract the two dates instead."""

	def __init__(self, date1, date2, alias=None):
		super().__init__("DATEDIFF", date1, date2, alias=alias)

	def as_postgres(self, **kwargs):
		return _PostgresDateDiff(*self.args, alias=self.alias).get_sql(**kwargs)

	# DATEDIFF() does not resolve on DuckDB; date - date does
	as_duckdb = as_postgres


class _MariaDBJSONExtract(Function):
	def __init__(self, field, path, **kwargs):
		super().__init__("JSON_EXTRACT", field, path, **kwargs)


class _MariaDBJSONValue(Function):
	def __init__(self, field, path, **kwargs):
		super().__init__("JSON_UNQUOTE", _MariaDBJSONExtract(field, path), **kwargs)


class _MariaDBJSONContains(Function):
	def __init__(self, target, candidate, **kwargs):
		from pypika.terms import JSON

		if not isinstance(candidate, Term):
			candidate = JSON(candidate)
		super().__init__("JSON_CONTAINS", target, candidate, **kwargs)


JSONExtract = ImportMapper(
	{
		db_type_is.MARIADB: _MariaDBJSONExtract,
		db_type_is.POSTGRES: lambda field, path, **kw: field.get_json_value(path),
		db_type_is.DUCKDB: lambda field, path, **kw: field.get_json_value(path),
	}
)

JSONValue = ImportMapper(
	{
		db_type_is.MARIADB: _MariaDBJSONValue,
		db_type_is.POSTGRES: lambda field, path, **kw: field.get_text_value(path),
		db_type_is.DUCKDB: lambda field, path, **kw: field.get_text_value(path),
	}
)

JSONContains = ImportMapper(
	{
		db_type_is.MARIADB: _MariaDBJSONContains,
		db_type_is.POSTGRES: lambda target, candidate, **kw: target.contains(candidate),
		db_type_is.DUCKDB: lambda target, candidate, **kw: Function("json_contains", target, candidate),
	}
)


class Cast_(Function):
	def __init__(self, value, as_type, alias=None):
		if frappe.db.db_type == "mariadb" and (
			(hasattr(as_type, "get_sql") and as_type.get_sql().lower() == "varchar")
			or str(as_type).lower() == "varchar"
		):
			# mimics varchar cast in mariadb
			# as mariadb doesn't have varchar data cast
			# https://mariadb.com/kb/en/cast/#description

			# ref: https://stackoverflow.com/a/32542095
			super().__init__("CONCAT", value, "", alias=alias)
		else:
			# from source: https://pypika.readthedocs.io/en/latest/_modules/pypika/functions.html#Cast
			super().__init__("CAST", value, alias=alias)
			self.as_type = as_type

	def get_special_params_sql(self, **kwargs):
		if self.name.lower() == "cast":
			type_sql = (
				self.as_type.get_sql(**kwargs)
				if hasattr(self.as_type, "get_sql")
				else str(self.as_type).upper()
			)
			return f"AS {type_sql}"


def _aggregate(function, dt, fieldname, filters, **kwargs):
	return (
		frappe.qb.get_query(dt, filters=filters, fields=[function(PseudoColumn(fieldname))]).run(**kwargs)[0][
			0
		]
		or 0
	)


class SqlFunctions(Enum):
	DayOfYear = "dayofyear"
	Extract = "extract"
	Locate = "locate"
	Count = "count"
	Sum = "sum"
	Avg = "avg"
	Max = "max"
	Min = "min"
	Abs = "abs"
	Timestamp = "timestamp"
	IfNull = "ifnull"


def _max(dt, fieldname, filters=None, **kwargs):
	return _aggregate(Max, dt, fieldname, filters, **kwargs)


def _min(dt, fieldname, filters=None, **kwargs):
	return _aggregate(Min, dt, fieldname, filters, **kwargs)


def _avg(dt, fieldname, filters=None, **kwargs):
	return _aggregate(Avg, dt, fieldname, filters, **kwargs)


def _sum(dt, fieldname, filters=None, **kwargs):
	return _aggregate(Sum, dt, fieldname, filters, **kwargs)
