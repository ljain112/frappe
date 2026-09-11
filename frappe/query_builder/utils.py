import functools
import inspect
import types
from enum import Enum
from importlib import import_module
from typing import Any, NamedTuple, get_type_hints

from pypika import Dialects
from pypika.enums import Comparator, Matching
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
	"""One database backend, as the query builder sees it."""

	#: the `db_type` name; `per_db_type` dispatches to `as_<name>()`
	name: str
	builder: type
	#: what pypika threads down the render tree as `dialect`. Its own members are kept rather
	#: than replaced because pypika reads them itself (`Array` renders ARRAY[...] on postgres;
	#: `Interval` and `Now` have sqlite spellings).
	pypika: Dialects


DB_TYPES = {
	db_type_is.MARIADB: DbType("mariadb", MariaDB, Dialects.MYSQL),
	db_type_is.POSTGRES: DbType("postgres", Postgres, Dialects.POSTGRESQL),
	db_type_is.SQLITE: DbType("sqlite", SQLite, Dialects.SQLLITE),
}

assert set(DB_TYPES) == set(db_type_is), "DB_TYPES must describe every db_type_is member"

# derived views; add a database as one row in DB_TYPES, never here
DB_TYPE_MAP = {key: backend.builder for key, backend in DB_TYPES.items()}
_DB_TYPE_NAMES = {backend.pypika: backend.name for backend in DB_TYPES.values()}


# the site's builder is the default for a term rendered outside a query -- a bare `term.get_sql()`
# in a test or a helper. Inside a query pypika supplies `dialect` at the root, so this is never
# consulted on a real query path. Keyed by builder so the fallback is exactly what `frappe.qb`
# would have used, with no config parsing per render.
_PYPIKA_BY_BUILDER = {backend.builder: backend.pypika for backend in DB_TYPES.values()}
_RENDERER_ATTRS = {name: f"as_{name}" for name in _DB_TYPE_NAMES.values()}


#: the spelling frappe's query builder is written in: every undecorated term, and the default
#: rendering of every `per_db_type` term unless it says otherwise
BASE_DB_TYPE = db_type_is.MARIADB.value


class UnsupportedOperation(NotImplementedError):
	"""A term has no rendering for the database it is being rendered for."""

	def __init__(self, term: type, db_type: str) -> None:
		super().__init__(
			f"{term.__name__} cannot be rendered for {db_type}: "
			f"define {term.__name__}.as_{db_type}() or list {db_type!r} in default_for"
		)


def per_db_type(cls=None, *, default_for: str | set[str] = BASE_DB_TYPE):
	"""Let a term render differently per database, following Django's `as_<vendor>()` convention.

	A term that renders the same everywhere needs nothing. A term whose spelling differs is
	decorated and defines `as_postgres()` / `as_sqlite()` / ... for each database that differs
	from its default rendering. Rendering picks `as_<db_type>()` when the class defines it, the
	default when the database is one `default_for` names, and raises `UnsupportedOperation`
	otherwise -- a database the term does not know is an error, not a silent wrong spelling.

	`default_for` says which databases the default rendering is written for. Left alone it is
	`BASE_DB_TYPE`, which is true of every term in frappe today; pass a set when the default is
	right on several (`default_for={"mariadb", "sqlite"}`) or `"*"` when it is right everywhere
	and the methods are exceptions.

	The database comes from the `dialect` pypika threads through the whole render tree, rather
	than from `frappe.conf.db_type` at construction: the same term object renders correctly
	wherever it is sent, and another database is an added method rather than an edit to a
	dispatch table.

	The decorator also keeps `self.raw_args` -- the operands as the caller passed them. pypika's
	`Function.__init__` wraps every operand in a `ValueWrapper` before an `as_*` method can look
	at it, so a rendering that must inspect an operand (cast a `str` to DATE, call a `Field`
	method) reads `raw_args`, not `self.args`.

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
