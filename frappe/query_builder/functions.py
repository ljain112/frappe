from datetime import time
from enum import Enum

from pypika.functions import *
from pypika.terms import Arithmetic, ArithmeticExpression, Function, Term
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
from frappe.query_builder.utils import per_db_type

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


@per_db_type
class Locate(Locate):
	"""LOCATE(needle, haystack), in each backend's own spelling."""

	def as_postgres(self, **kwargs):
		return Strpos(*self.args, alias=self.alias).get_sql(**kwargs)

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


@per_db_type
class GroupConcat(GROUP_CONCAT):
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


@per_db_type
class Match(MATCH):
	"""Full-text search: MATCH ... AGAINST on mariadb, to_tsvector on postgres."""

	def as_postgres(self, **kwargs):
		return TO_TSVECTOR(self.args[0], alias=self.alias).Against(self._Against).get_sql(**kwargs)


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


@per_db_type
class CombineDatetime(Function):
	"""TIMESTAMP(date, time); postgres adds the two parts instead."""

	def __init__(self, date, time, alias=None):
		super().__init__("TIMESTAMP", date, time, alias=alias)

	def as_postgres(self, **kwargs):
		# raw_args: _PostgresTimestamp casts a `str` / `time` operand, which it cannot see once wrapped
		return _PostgresTimestamp(*self.raw_args, alias=self.alias).get_sql(**kwargs)


@per_db_type
class DateFormat(Function):
	"""DATE_FORMAT(date, format), in each backend's own spelling."""

	def __init__(self, date, format, alias=None):
		super().__init__("DATE_FORMAT", date, format, alias=alias)

	def as_postgres(self, **kwargs):
		return ToChar(*self.args, alias=self.alias).get_sql(**kwargs)


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


@per_db_type
class UnixTimestamp(Function):
	"""unix_timestamp(date); elsewhere an epoch extraction."""

	def __init__(self, date, alias=None):
		super().__init__("unix_timestamp", date, alias=alias)

	def as_postgres(self, **kwargs):
		return _PostgresUnixTimestamp(*self.args, alias=self.alias).get_sql(**kwargs)


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


@per_db_type
class DateDiff(Function):
	"""DATEDIFF(date1, date2); postgres subtracts the two dates instead."""

	def __init__(self, date1, date2, alias=None):
		super().__init__("DATEDIFF", date1, date2, alias=alias)

	def as_postgres(self, **kwargs):
		return _PostgresDateDiff(*self.args, alias=self.alias).get_sql(**kwargs)


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


@per_db_type
class JSONExtract(_MariaDBJSONExtract):
	"""JSON_EXTRACT(field, path); postgres has the `->` operator.

	The postgres renderings of the three JSON terms are pypika operators built from the `Field`
	itself, so they read `raw_args` rather than the wrapped `args`.
	"""

	def as_postgres(self, **kwargs):
		field, path = self.raw_args
		return field.get_json_value(path).get_sql(**kwargs)


@per_db_type
class JSONValue(_MariaDBJSONValue):
	"""JSON_UNQUOTE(JSON_EXTRACT(field, path)); postgres has the `->>` operator."""

	def as_postgres(self, **kwargs):
		field, path = self.raw_args
		return field.get_text_value(path).get_sql(**kwargs)


@per_db_type
class JSONContains(_MariaDBJSONContains):
	"""JSON_CONTAINS(target, candidate); postgres has the `@>` operator."""

	def as_postgres(self, **kwargs):
		target, candidate = self.raw_args
		return target.contains(candidate).get_sql(**kwargs)


@per_db_type(default_for="*")
class Cast_(Function):
	"""CAST(value AS type).

	MariaDB has no VARCHAR cast (https://mariadb.com/kb/en/cast/#description), so a varchar cast
	is spelled `CONCAT(value, '')` there -- ref https://stackoverflow.com/a/32542095. Every other
	database, and every other type, takes the plain CAST.
	"""

	def __init__(self, value, as_type, alias=None):
		# from source: https://pypika.readthedocs.io/en/latest/_modules/pypika/functions.html#Cast
		super().__init__("CAST", value, alias=alias)
		self.as_type = as_type

	def get_special_params_sql(self, **kwargs):
		type_sql = (
			self.as_type.get_sql(**kwargs) if hasattr(self.as_type, "get_sql") else str(self.as_type).upper()
		)
		return f"AS {type_sql}"

	def as_mariadb(self, **kwargs):
		type_name = self.as_type.get_sql() if hasattr(self.as_type, "get_sql") else str(self.as_type)
		if type_name.lower() != "varchar":
			return super().get_sql(**kwargs)
		return Function("CONCAT", self.raw_args[0], "", alias=self.alias).get_sql(**kwargs)


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
