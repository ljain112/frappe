import inspect
from collections.abc import Callable, Iterator
from enum import Enum
from functools import partial
from importlib import import_module
from typing import Any, NamedTuple, get_type_hints

from pypika import Dialects
from pypika.queries import Column, QueryBuilder, _SetOperation
from pypika.terms import PseudoColumn

import frappe
from frappe.query_builder.terms import DuckDBParameterWrapper, NamedParameterWrapper

from .builder import Base, DuckDB, MariaDB, Postgres, SQLite


class db_type_is(Enum):
	MARIADB = "mariadb"
	POSTGRES = "postgres"
	SQLITE = "sqlite"
	DUCKDB = "duckdb"


# Frappe's name for the dialect a query is *being rendered for*, which is not always the site's
# own -- a snapshot report builds its query with the site's dialect and renders it again for
# another one.
#
# pypika already threads `dialect` down the whole render tree (`QueryBuilder.get_sql` does
# `kwargs.setdefault("dialect", self.dialect)`), so nothing needs to carry it separately. Its own
# `Dialects` members are left in place rather than replaced, because pypika reads them itself --
# `Array` renders ARRAY[...] on postgres, and `Interval` and `Now` have sqlite spellings. This
# maps whatever arrives to the name frappe uses, so terms dispatch on `mariadb` / `postgres` /
# `sqlite` / `duckdb` instead of pypika's vocabulary. A dialect with no pypika member (DuckDB)
# supplies its own `db_type_is` marker, which pypika carries through untouched and none of its
# three `Dialects` comparisons match -- the correct answer for DuckDB in each case.
DIALECT_NAMES = {
	Dialects.MYSQL: db_type_is.MARIADB.value,
	Dialects.POSTGRESQL: db_type_is.POSTGRES.value,
	Dialects.SQLLITE: db_type_is.SQLITE.value,
	db_type_is.DUCKDB: db_type_is.DUCKDB.value,
}


class Dialect(NamedTuple):
	"""What rendering a query needs to know about a backend.

	Everything here is read by `prepare_query`, so a new backend is an entry in `DB_TYPE_MAP`
	rather than another branch inside it.
	"""

	builder: type
	quote_char: str
	#: driver placeholder style; DuckDB rejects the pyformat `%(name)s` the others use
	parameter_wrapper: type = NamedParameterWrapper
	#: FORCE INDEX / USE INDEX are MySQL-only and a syntax error elsewhere
	supports_index_hints: bool = False
	#: whether a selected column must appear in GROUP BY or an aggregate
	strict_group_by: bool = False


DB_TYPE_MAP = {
	db_type_is.MARIADB: Dialect(MariaDB, "`", supports_index_hints=True),
	db_type_is.POSTGRES: Dialect(Postgres, '"', strict_group_by=True),
	db_type_is.SQLITE: Dialect(SQLite, '"'),
	db_type_is.DUCKDB: Dialect(DuckDB, '"', DuckDBParameterWrapper, strict_group_by=True),
}

assert set(DB_TYPE_MAP) == set(db_type_is), "DB_TYPE_MAP must map every db_type_is member to a dialect"


class DialectTerm:
	"""Render per target dialect, following Django's `as_<vendor>()` convention.

	A term defines its default rendering and, optionally, an `as_mariadb()` / `as_postgres()` /
	`as_sqlite()` / `as_duckdb()` method; `get_sql` dispatches to that method when the class
	defines one and falls back to the default otherwise -- the same contract as Django's
	`Expression.as_sql()` / `as_vendorname()`, and the same idea as SQLAlchemy's
	`@compiles(Element, "postgresql")`.

	A term with no method for the dialect in hand therefore renders exactly as it always did,
	which is what keeps every query off an alternate target byte-identical.
	"""

	def get_sql(self, **kwargs: Any) -> str:
		dialect = DIALECT_NAMES.get(kwargs.get("dialect"))
		renderer = getattr(self, f"as_{dialect}", None) if dialect else None
		return renderer(**kwargs) if renderer else super().get_sql(**kwargs)


class PseudoColumnMapper(PseudoColumn):
	def __init__(self, name: str) -> None:
		super().__init__(name)

	def get_sql(self, **kwargs):
		if frappe.db.db_type == "postgres":
			self.name = self.name.replace("`", '"')
		return self.name


class ImportMapper:
	def __init__(self, func_map: dict[db_type_is, Callable]) -> None:
		self.func_map = func_map

	def __call__(self, *args: Any, **kwds: Any) -> Callable:
		db = db_type_is(frappe.conf.db_type)
		return self.func_map[db](*args, **kwds)


class BuilderIdentificationFailed(Exception):
	def __init__(self):
		super().__init__("Couldn't guess builder")


def get_query_builder(type_of_db: str) -> Postgres | MariaDB | SQLite:
	"""Return the query builder object.

	Args:
	        type_of_db: string value of the db used
	"""
	return DB_TYPE_MAP[db_type_is(type_of_db)].builder


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


def get_query_target(query):
	"""Return an active alternate read target that can serve `query`, or None.

	A target is registered on `frappe.local.query_targets` by whichever backend opened it -- a
	DuckDB snapshot today -- and answers `can_serve` for the set of tables a query reads. Nothing
	here knows what backend it is talking to.

	A target typically holds a subset of the site's tables, so a query that also reads something
	outside it must keep going to the site database. Rather than make callers say which is which,
	route on what the query reads: a query spanning a target and anything outside it falls back,
	which is correct, just not accelerated.

	This is a cheap first pass, not a guarantee. Subqueries are deliberately not walked -- pypika
	terms are only as traversable as their `nodes_()`, and the ones that matter hide what they
	wrap (`ExistsCriterion` does not expose its container at all, `RawCriterion` is opaque SQL by
	definition), so a walk would be unreliable while reading as though it were exhaustive.
	`execute_query` instead lets the target itself reject what it cannot answer, which is
	authoritative.
	"""
	targets = getattr(frappe.local, "query_targets", None)
	if not targets:
		return None

	# `_SetOperation` (UNION) shares this run() and has no _from/_joins
	tables = {table.get_sql() for table in getattr(query, "_from", ())}
	tables.update(join.item.get_sql() for join in getattr(query, "_joins", ()))
	if not tables:
		return None

	for target in targets:
		if target.can_serve(tables):
			return target

	return None


def requires_strict_group_by() -> bool:
	"""Whether a query must be built so a selected column appears in GROUP BY or an aggregate.

	True when the site's own dialect requires it, and also when an alternate target's does -- a
	query re-rendered for that target has to be valid there too, and the stricter form is valid
	on both.
	"""
	if DB_TYPE_MAP[db_type_is(frappe.conf.db_type)].strict_group_by:
		return True

	targets = getattr(frappe.local, "query_targets", None)
	return any(DB_TYPE_MAP[target.dialect].strict_group_by for target in targets or ())


def render_for_dialect(query, spec: Dialect, dialect):
	"""Render `query` for a dialect other than the site's own.

	Everything that differs is read off `spec`, so this stays the same function whatever the
	target is.
	"""
	param_collector = spec.parameter_wrapper()

	# A query built for the site database can carry index hints, which are MySQL-only syntax.
	# Callers already skip them on postgres for the same reason (see erpnext's
	# financial_statements.set_gl_entries_by_account), but on a mariadb site the hint is part of
	# the query by the time it is re-rendered here, so drop it for this render only.
	indexes = {}
	if not spec.supports_index_hints:
		indexes = {
			attr: getattr(query, attr)
			for attr in ("_force_indexes", "_use_indexes")
			if getattr(query, attr, None)
		}
		for attr in indexes:
			setattr(query, attr, [])

	try:
		sql = query.get_sql(param_wrapper=param_collector, quote_char=spec.quote_char, dialect=dialect)
	finally:
		for attr, value in indexes.items():
			setattr(query, attr, value)

	# `RawCriterion` returns hook-supplied permission SQL verbatim and ignores quote_char, so the
	# site's own quoting can still reach here. Rewrite it the way postgres's modify_query does.
	if spec.quote_char != "`":
		sql = sql.replace("`", spec.quote_char)

	assert isinstance(sql, str), "prepared query must be a SQL string"
	return sql, param_collector.get_parameters(), select_column_names(query)


def select_column_names(query):
	"""Name the columns the way the site database would, or None to fall back to the driver.

	A driver names an unaliased expression after its own SQL text, so mariadb reports
	``SUM(`amount`)`` where DuckDB reports ``sum(amount)`` -- a `frappe.get_all(fields=[{"SUM":
	"amount"}])` on a snapshot would otherwise come back keyed differently than the same call off
	it. Rendering the select terms in the site's own dialect reproduces its naming; an aliased
	term is already named by its alias on both.
	"""
	from pypika.terms import Field

	selects = getattr(query, "_selects", None)
	if not selects:
		return None

	quote_char = frappe.local.qb._BuilderClasss.QUOTE_CHAR
	names = []

	for term in selects:
		if term.alias:
			names.append(term.alias)
		elif isinstance(term, Field):
			# a plain column is reported by its bare name, not its quoted SQL
			names.append(term.name)
		else:
			names.append(term.get_sql(quote_char=quote_char, with_alias=False))

	return names


def execute_query(query, *args, **kwargs):
	dt = query.__dict__.get("_doctype")
	parent_dt = query.__dict__.get("_parent_doctype")
	fields = query.__dict__.get("_fields_list", [])
	child_queries = query._child_queries

	target = get_query_target(query)
	if target is not None:
		try:
			result = target.execute(*prepare_query(query, dialect=target.dialect), **kwargs)
		except target.fallback_errors:
			# Either the target does not hold some table or column this query needs -- the
			# structural check in get_query_target cannot see an ExistsCriterion subquery or raw
			# permission SQL -- or the query is legal on the site database but not there. Both
			# fail while binding, before any rows are read, so answer from the site database
			# instead. A query the site database also rejects raises there, exactly as it would
			# with no target open.
			target = None

	if target is None:
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


def prepare_query(query, dialect: "db_type_is | None" = None):
	"""Render `query` as SQL plus its parameters, for the site's dialect or another one.

	`dialect` is only passed when rendering for something other than the site database -- a
	snapshot report renders the query it already built for DuckDB. Left as None the render call
	is exactly what it has always been, so the site's own SQL is unchanged.
	"""
	from frappe.utils.safe_exec import SERVER_SCRIPT_FILE_PREFIX, check_safe_sql_query

	if dialect is not None:
		return render_for_dialect(query, DB_TYPE_MAP[dialect], dialect)

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


def patch_like_operators():
	"""Render the query-builder LIKE / NOT LIKE operators as ILIKE / NOT ILIKE on postgres.

	MariaDB's default collation makes LIKE case-insensitive; postgres and DuckDB compare text
	case-sensitively, so a `.like()` search (link-field autocomplete, etc.) would only match
	exact case there. Mapping to ILIKE keeps pattern matching case-insensitive on every backend
	-- matching MariaDB and the like->ilike translation `frappe.db.get_list` already applies for
	its filter path.

	On a snapshot the spelling is not cosmetic: a plain LIKE re-rendered for DuckDB returns
	*fewer rows* than the same report on MariaDB rather than raising, so it has to be chosen when
	the query is rendered rather than when it is built.
	"""
	# pypika has no hook for dialect-specific operator rendering, so patch Term.like/not_like the
	# same way the query-builder patches above (QueryBuilder.run, Base.max, ...) and app.py's
	# Request.max_form_memory_size do. The rule anchors on the import, so suppress it there too.
	from pypika.enums import Matching
	from pypika.terms import BasicCriterion, Term  # nosemgrep: frappe-monkey-patching-not-allowed

	class Like(DialectTerm, BasicCriterion):
		"""LIKE, rendered as ILIKE wherever LIKE is case-sensitive."""

		def __init__(self, term, expr, negate=False):
			comparator = Matching.not_like if negate else Matching.like
			super().__init__(comparator, term, term.wrap_constant(expr))
			self.negate = negate

		def as_postgres(self, **kwargs):
			comparator = Matching.not_ilike if self.negate else Matching.ilike
			return BasicCriterion(comparator, self.left, self.right).get_sql(**kwargs)

		as_duckdb = as_postgres

	def like(self, expr: str):
		return Like(self, expr)

	def not_like(self, expr: str):
		return Like(self, expr, negate=True)

	Term.like = like  # nosemgrep: frappe-monkey-patching-not-allowed
	Term.not_like = not_like  # nosemgrep: frappe-monkey-patching-not-allowed


def patch_regex_operator():
	"""Render the query-builder regex operator in each backend's native spelling.

	pypika's Term.regex emits " REGEX ", which is an operator on no backend: MySQL spells it
	REGEXP, postgres uses the case-insensitive match ~*, and DuckDB has neither -- REGEXP is a
	parser error there and ~* an unknown function, so it uses `regexp_matches(field, pattern,
	'i')`. So `frappe.get_all(filters={"f": ["regex", ...]})` produced a syntax error everywhere.
	Emitting the right operator here also means a generated query no longer depends on the
	textual REGEXP rewrite in modify_query.
	"""
	# pypika has no hook for dialect-specific operator rendering, so patch Term.regex the same way
	# patch_like_operators above does. The rule anchors on the import, so suppress it there too.
	from pypika.enums import Comparator, Matching
	from pypika.terms import BasicCriterion, Function, Term  # nosemgrep: frappe-monkey-patching-not-allowed

	class PostgresMatching(Comparator):
		regex = " ~* "

	class Regex(DialectTerm, BasicCriterion):
		"""REGEXP, rendered in each backend's own spelling."""

		def __init__(self, term, pattern):
			super().__init__(Matching.regexp, term, term.wrap_constant(pattern))

		def as_postgres(self, **kwargs):
			return BasicCriterion(PostgresMatching.regex, self.left, self.right).get_sql(**kwargs)

		def as_duckdb(self, **kwargs):
			return Function("regexp_matches", self.left, self.right, "i").get_sql(**kwargs)

	def regex(self, pattern: str):
		return Regex(self, pattern)

	Term.regex = regex  # nosemgrep: frappe-monkey-patching-not-allowed


def patch_all():
	patch_query_execute()
	patch_query_aggregation()
	patch_get_query()
	patch_like_operators()
	patch_regex_operator()
