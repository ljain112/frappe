import copy
import functools
import inspect
import types
import warnings
from enum import Enum
from importlib import import_module
from typing import Any, NamedTuple, get_type_hints

from pypika import Dialects
from pypika.enums import Comparator, Matching
from pypika.functions import Max
from pypika.queries import Column, QueryBuilder, _SetOperation
from pypika.terms import BasicCriterion, PseudoColumn

import frappe
from frappe.query_builder.terms import NamedParameterWrapper

from .builder import Base, MariaDB, Postgres, SQLite


class db_type_is(Enum):
	MARIADB = "mariadb"
	POSTGRES = "postgres"
	SQLITE = "sqlite"


class DbType(NamedTuple):
	"""One database, as the query builder sees it. The flags are read only by `compile_query`."""

	#: the `db_type` name; `per_db_type` dispatches to `as_<name>()`
	name: str
	builder: type
	#: pypika's dialect for this database; kept because pypika reads it itself (Array, Interval, Now)
	pypika: Dialects
	#: OFFSET is a syntax error without a LIMIT before it
	offset_requires_limit: bool = False
	#: with DISTINCT, every ORDER BY column must appear in the select list
	distinct_order_by_must_be_selected: bool = False
	#: a selected column must be grouped or aggregated (functional-dependency rule)
	strict_group_by: bool = False
	#: FORCE INDEX / USE INDEX are accepted
	supports_index_hints: bool = False


DB_TYPES = {
	db_type_is.MARIADB: DbType(
		"mariadb",
		MariaDB,
		Dialects.MYSQL,
		offset_requires_limit=True,
		supports_index_hints=True,
	),
	db_type_is.POSTGRES: DbType(
		"postgres",
		Postgres,
		Dialects.POSTGRESQL,
		distinct_order_by_must_be_selected=True,
		strict_group_by=True,
	),
	db_type_is.SQLITE: DbType("sqlite", SQLite, Dialects.SQLLITE, offset_requires_limit=True),
}

assert set(DB_TYPES) == set(db_type_is), "DB_TYPES must describe every db_type_is member"

# derived views; add a database as one row in DB_TYPES, never here
DB_TYPE_MAP = {key: backend.builder for key, backend in DB_TYPES.items()}
_DB_TYPE_NAMES = {backend.pypika: backend.name for backend in DB_TYPES.values()}
_DB_TYPES_BY_NAME = {backend.name: backend for backend in DB_TYPES.values()}

#: the largest LIMIT MariaDB accepts; stands in for "no limit" where OFFSET needs one
MAX_LIMIT = 18446744073709551615


class QueryShape(NamedTuple):
	"""What a stricter database would need of a query: recorded by `Engine` at build, applied by
	`SHAPE_RULES` at render. Describes the tree as built; later `.orderby()` / `.select()` calls
	are not reflected."""

	#: DISTINCT with an ORDER BY column that is not in the select list
	order_by_unselected: bool = False
	#: primary keys of 1:1 joined link tables, to add to a GROUP BY on the main table's key
	group_by_extension: tuple = ()
	#: id() of ORDER BY terms an aggregate query must wrap in Max()
	aggregate_order_terms: frozenset = frozenset()


def _shape_of(query) -> QueryShape:
	# vars(), not getattr(): a pypika builder returns a Field for any unknown attribute
	return vars(query).get("_shape") or QueryShape()


def limit_before_offset(query, spec: DbType):
	if spec.offset_requires_limit and query._offset and query._limit is None:
		query = copy.copy(query)
		query._limit = MAX_LIMIT
	return query


def drop_unselected_order_by(query, spec: DbType):
	if not (spec.distinct_order_by_must_be_selected and query._distinct and query._orderbys):
		return query
	if not _shape_of(query).order_by_unselected:
		return query
	query = copy.copy(query)
	query._orderbys = []
	warnings.warn(
		"ORDER BY fields have been ignored because PostgreSQL requires them to "
		"appear in the SELECT list when using with DISTINCT",
		UserWarning,
		stacklevel=5,
	)
	return query


def extend_group_by(query, spec: DbType):
	if spec.strict_group_by and (extension := _shape_of(query).group_by_extension):
		query = copy.copy(query)
		query._groupbys = [*query._groupbys, *extension]
	return query


def aggregate_order_by(query, spec: DbType):
	if spec.strict_group_by and (terms := _shape_of(query).aggregate_order_terms):
		query = copy.copy(query)
		query._orderbys = [
			(Max(term) if id(term) in terms else term, order) for term, order in query._orderbys
		]
	return query


def strip_index_hints(query, spec: DbType):
	if not spec.supports_index_hints and (query._force_indexes or query._use_indexes):
		query = copy.copy(query)
		query._force_indexes, query._use_indexes = [], []
	return query


#: applied in this order; each returns the query untouched unless the database's flag asks for a change
SHAPE_RULES = (
	limit_before_offset,
	drop_unselected_order_by,
	extend_group_by,
	aggregate_order_by,
	strip_index_hints,
)


def compile_query(query, dialect=None):
	"""Shape `query` for the database it is about to be rendered for.

	A rule that changes something works on a shallow copy, so the original renders again
	unchanged. The only reader of the `DbType` flags. An unknown dialect raises rather than rendering unshaped SQL.
	"""
	dialect = dialect or vars(query).get("dialect")
	spec = _DB_TYPES_BY_NAME.get(_DB_TYPE_NAMES.get(dialect))
	if spec is None:
		raise UnsupportedOperation(type(query), str(dialect), hint="add a DbType row for it in DB_TYPES")

	# each rule copies only when it changes something, so the common case renders the tree as is
	for rule in SHAPE_RULES:
		query = rule(query, spec)
	return query


# default dialect for a term rendered outside a query (a bare `term.get_sql()`): the site's
# builder. Inside a query pypika supplies `dialect` at the root.
_PYPIKA_BY_BUILDER = {backend.builder: backend.pypika for backend in DB_TYPES.values()}
_RENDERER_ATTRS = {name: f"as_{name}" for name in _DB_TYPE_NAMES.values()}


#: the spelling frappe's query builder is written in: every undecorated term, and the default
#: rendering of every `per_db_type` term unless it says otherwise
BASE_DB_TYPE = db_type_is.MARIADB.value


class UnsupportedOperation(NotImplementedError):
	"""A term has no rendering for the database it is being rendered for."""

	def __init__(self, term: type, db_type: str, hint: str | None = None) -> None:
		hint = hint or f"define {term.__name__}.as_{db_type}() or list {db_type!r} in default_for"
		super().__init__(f"{term.__name__} cannot be rendered for {db_type}: {hint}")


def per_db_type(cls=None, *, default_for: str | set[str] = BASE_DB_TYPE):
	"""Render a term per database, following Django's `as_<vendor>()` convention.

	Decorate a term whose spelling differs and define `as_postgres()` / `as_sqlite()` / ... for
	each database that differs from the default rendering. `default_for` names the databases the
	default is written for: `BASE_DB_TYPE` unless given, a set, or `"*"` for all. A database with
	no method and not in `default_for` raises `UnsupportedOperation`.

	The database is the `dialect` pypika passes at render, never `frappe.conf.db_type` at
	construction. `self.raw_args` keeps the operands as passed, because pypika wraps them before
	an `as_*` method can inspect them.

	    @per_db_type
	    class GroupConcat(GROUP_CONCAT):
	        def as_postgres(self, **kwargs): ...
	"""
	defaults = (
		None if default_for == "*" else {default_for} if isinstance(default_for, str) else set(default_for)
	)

	def decorate(cls):
		init, default = cls.__init__, cls.get_sql

		@functools.wraps(init)
		def __init__(self, *args, **kwargs):
			self.raw_args = args
			init(self, *args, **kwargs)

		@functools.wraps(default)
		def get_sql(self, **kwargs: Any) -> str:
			dialect = kwargs.get("dialect") or _PYPIKA_BY_BUILDER.get(getattr(frappe.local, "qb", None))
			name = _DB_TYPE_NAMES.get(dialect)
			if name is None:
				return default(self, **kwargs)
			if renderer := getattr(self, _RENDERER_ATTRS[name], None):
				return renderer(**kwargs)
			if defaults is None or name in defaults:
				return default(self, **kwargs)
			raise UnsupportedOperation(cls, name)

		cls.__init__, cls.get_sql = __init__, get_sql
		return cls

	return decorate(cls) if cls is not None else decorate


@per_db_type(default_for={"mariadb", "sqlite"})
class PseudoColumnMapper(PseudoColumn):
	def __init__(self, name: str) -> None:
		super().__init__(name)

	def as_postgres(self, **kwargs):
		# Returned, not assigned to `self.name`: rendering must not mutate the term, or a
		# pseudo-column rendered once on postgres renders wrongly everywhere after.
		from frappe.database.utils import convert_backtick_identifiers

		return convert_backtick_identifiers(self.name)


class BuilderIdentificationFailed(Exception):
	def __init__(self):
		super().__init__("Couldn't guess builder")


def get_query_builder(type_of_db: str) -> Postgres | MariaDB | SQLite:
	"""Return the query builder object.

	Args:
	        type_of_db: string value of the db used
	"""
	return DB_TYPE_MAP[db_type_is(type_of_db)]


def get_query(*args, **kwargs) -> QueryBuilder:
	from frappe.database.query import Engine

	return Engine().get_query(*args, **kwargs)


def get_attr(method_string):
	modulename = ".".join(method_string.split(".")[:-1])
	methodname = method_string.split(".")[-1]
	return getattr(import_module(modulename), methodname)


def DocType(*args, **kwargs):
	return frappe.qb.DocType(*args, **kwargs)


def Table(*args, **kwargs):
	return frappe.qb.Table(*args, **kwargs)


def mask_fields(
	doctype: str,
	fields: list[Any],
	result: list[dict] | list[tuple],
	as_dict: bool = True,
	pluck: bool = False,
	parent_doctype: str | None = None,
) -> list[dict] | list[tuple]:
	"""Mask fields in the result based on the doctype's masked fields.

	Args:
		doctype: Name of the DocType being queried
		fields: List of field objects from the query
		result: Query results as list of dicts or tuples
		as_dict: Whether results are dictionaries (True) or tuples (False)
		pluck: Whether results were plucked into a flat list of scalar values
		parent_doctype: Parent DocType when querying a child table, used to
			resolve role permissions for the `mask` permission type
	Returns:
		Result with masked field values applied based on user permissions
	"""
	from frappe.database.query import CORE_DOCTYPES
	from frappe.model.utils.mask import mask_dict_results, mask_list_results, mask_pluck_results

	# We can't query meta for core doctypes here
	if doctype in CORE_DOCTYPES:
		return result

	masked_fields = frappe.get_meta(doctype).get_masked_fields(
		parenttype=parent_doctype
	) + get_masked_joined_fields(doctype, fields)

	if not masked_fields:
		return result

	if pluck:
		return mask_pluck_results(result, masked_fields, fields)

	if not as_dict:
		field_index_map = {}
		for idx, field in enumerate(fields):
			# Handle aliases (e.g. `tabSI`.`posting_date` as posting_date)
			if alias := getattr(field, "alias", None):
				field_index_map[alias] = idx
			elif name := getattr(field, "name", None) or getattr(field, "fieldname", None):
				field_index_map[name] = idx

		return mask_list_results(result, masked_fields, field_index_map)

	# Handle as_dict format
	return mask_dict_results(result, masked_fields)


def get_masked_joined_fields(doctype: str, fields: list[Any]) -> list[Any]:
	"""Get masked fields of the doctypes joined in through dot notation (`items.rate`)."""
	from frappe.database.query import CORE_DOCTYPES, DynamicTableField
	from frappe.model.utils.mask import as_aliased_field

	masked_fields = []
	lookups = {}

	for field in fields:
		if not isinstance(field, DynamicTableField) or field.doctype in CORE_DOCTYPES:
			continue

		if field.doctype not in lookups:
			meta = frappe.get_meta(field.doctype)
			parenttype = doctype if meta.istable else None
			lookups[field.doctype] = {
				df.fieldname: df for df in meta.get_masked_fields(parenttype=parenttype)
			}

		if df := lookups[field.doctype].get(field.fieldname):
			masked_fields.append(as_aliased_field(df, field.alias))

	return masked_fields


def execute_query(query, *args, **kwargs):
	dt = query.__dict__.get("_doctype")
	parent_dt = query.__dict__.get("_parent_doctype")
	fields = query.__dict__.get("_fields_list", [])
	child_queries = query._child_queries
	name_field_injected = query.__dict__.get("_name_field_injected", False)
	query, params = prepare_query(query)
	result = frappe.local.db.sql(query, params, *args, **kwargs)  # nosemgrep

	if child_queries and isinstance(child_queries, list) and result:
		execute_child_queries(child_queries, result)
		if dt:
			mask_child_query_fields(child_queries, result)

	if result and dt and fields:
		# `db.sql` returns tuples unless `as_dict` is passed, so masking must not assume dicts
		as_dict = bool(kwargs.get("as_dict"))
		result = mask_fields(
			dt, fields, result, as_dict=as_dict, pluck=kwargs.get("pluck", False), parent_doctype=parent_dt
		)

	if name_field_injected and result and not kwargs.get("pluck"):
		if isinstance(result[0], dict):
			for row in result:
				row.pop("name", None)
		else:
			if isinstance(result, tuple):
				result = tuple(row[:-1] for row in result)
			else:
				result = [row[:-1] for row in result]

	return result


def mask_child_query_fields(child_queries, result):
	if not isinstance(result[0], dict):
		return

	from frappe.database.query import CORE_DOCTYPES
	from frappe.model.utils.mask import mask_dict_results

	for child_query in child_queries:
		if child_query.doctype in CORE_DOCTYPES:
			continue
		masked_fields = frappe.get_meta(child_query.doctype).get_masked_fields(
			parenttype=child_query.parent_doctype
		)
		if not masked_fields:
			continue
		for row in result:
			mask_dict_results(row.get(child_query.fieldname) or [], masked_fields)


def execute_child_queries(queries, result):
	if not isinstance(result[0], dict) or not result[0].name:
		return
	parent_names = [d.name for d in result]
	for child_query in queries:
		data = child_query.get_query(parent_names).run(as_dict=1)
		for row in result:
			row[child_query.fieldname] = []
			for d in data:
				if str(d.parent) == str(row.name) and d.parentfield == child_query.fieldname:
					if "parent" not in child_query.fields:
						del d["parent"]
					if "parentfield" not in child_query.fields:
						del d["parentfield"]
					row[child_query.fieldname].append(d)


def prepare_query(query):
	from frappe.utils.safe_exec import SERVER_SCRIPT_FILE_PREFIX, check_safe_sql_query

	param_collector = NamedParameterWrapper()
	query = query.get_sql(param_wrapper=param_collector)
	if frappe.local.flags.get("in_safe_exec", False):
		if not check_safe_sql_query(query, throw=False):
			callstack = inspect.stack()

			# This check is required because QB can execute from anywhere and we can not
			# reliably provide a safe version for it in server scripts.

			# since query objects are patched everywhere any query.run()
			# will have callstack like this:
			# frame0: this function prepare_query()
			# frame1: execute_query()
			# frame2: frame that called `query.run()`
			#
			# if frame2 is server script <serverscript> is set as the filename it shouldn't be allowed.
			if len(callstack) >= 3 and SERVER_SCRIPT_FILE_PREFIX in callstack[2].filename:
				raise frappe.PermissionError("Only SELECT SQL allowed in scripting")

	if frappe.local.flags.get("in_render_safe_exec", False):
		check_safe_sql_query(query, throw=True)

	assert isinstance(query, str), "prepared query must be a SQL string"
	return query, param_collector.parameters


def patch_query_execute():
	"""Patch the Query Builder with helper execute method
	This excludes the use of `frappe.db.sql` method while
	executing the query object
	"""

	QueryBuilder.run = execute_query
	QueryBuilder.walk = prepare_query

	# To support running union queries
	_SetOperation.run = execute_query
	_SetOperation.walk = prepare_query


def patch_query_aggregation():
	"""Patch aggregation functions to frappe.qb"""
	from frappe.query_builder.functions import _avg, _max, _min, _sum

	Base.max = _max
	Base.min = _min
	Base.avg = _avg
	Base.sum = _sum


def patch_get_query():
	Base.get_query = get_query


class PostgresMatching(Comparator):
	regex = " ~* "


@per_db_type(default_for="*")
class LikeCriterion(BasicCriterion):
	"""LIKE / NOT LIKE, rendered ILIKE / NOT ILIKE on postgres.

	MariaDB's default collation makes LIKE case-insensitive (and so does SQLite's, for ASCII);
	postgres compares text case-sensitively, so a `.like()` search (link-field autocomplete,
	etc.) would only match exact case there. Mapping to ILIKE keeps pattern matching
	case-insensitive on every backend -- matching the like->ilike translation
	`frappe.db.get_list` already applies for its filter path.
	"""

	_POSTGRES = types.MappingProxyType({Matching.like: Matching.ilike, Matching.not_like: Matching.not_ilike})

	def as_postgres(self, **kwargs):
		comparator = self._POSTGRES.get(self.comparator, self.comparator)
		return BasicCriterion(comparator, self.left, self.right, alias=self.alias).get_sql(**kwargs)


@per_db_type(default_for={"mariadb", "sqlite"})
class RegexCriterion(BasicCriterion):
	"""The regex operator in each backend's native spelling.

	pypika's Term.regex emits " REGEX ", which is an operator on neither backend: MySQL and SQLite
	spell it REGEXP, postgres uses the case-insensitive match ~*. Emitting the right operator here
	also means a generated query no longer depends on the textual REGEXP rewrite in modify_query.
	"""

	def as_postgres(self, **kwargs):
		return BasicCriterion(PostgresMatching.regex, self.left, self.right, alias=self.alias).get_sql(
			**kwargs
		)


def patch_term_operators():
	"""Make Term.like / not_like / regex build the criteria above.

	pypika has no hook for backend-specific operator rendering, so patch Term the same way the
	query-builder patches above (QueryBuilder.run, Base.max, ...) and app.py's
	Request.max_form_memory_size do. The rule anchors on the import, so suppress it there too.
	The criteria themselves decide the spelling at render time, so nothing here reads db_type.
	"""
	from pypika.terms import Term  # nosemgrep: frappe-monkey-patching-not-allowed

	def like(self, expr: str):
		return LikeCriterion(Matching.like, self, self.wrap_constant(expr))

	def not_like(self, expr: str):
		return LikeCriterion(Matching.not_like, self, self.wrap_constant(expr))

	def regex(self, pattern: str):
		return RegexCriterion(Matching.regexp, self, self.wrap_constant(pattern))

	Term.like = like  # nosemgrep: frappe-monkey-patching-not-allowed
	Term.not_like = not_like  # nosemgrep: frappe-monkey-patching-not-allowed
	Term.regex = regex  # nosemgrep: frappe-monkey-patching-not-allowed


def patch_all():
	patch_query_execute()
	patch_query_aggregation()
	patch_get_query()
	patch_term_operators()
