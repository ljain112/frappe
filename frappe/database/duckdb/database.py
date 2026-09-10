import re
from contextlib import contextmanager
from datetime import datetime, time, timedelta

import frappe
from frappe.database import duckdb_file_path, get_duckdb
from frappe.database.database import Database
from frappe.database.duckdb.schema import DuckDBTable
from frappe.database.utils import get_doctype_name
from frappe.query_builder import Order
from frappe.query_builder.utils import db_type_is
from frappe.utils import get_table_name

MISSING_TABLE = re.compile(r"Table with name (.+?) does not exist")


def get_type_map():
	return {
		"Currency": ("decimal", "21,9"),
		"Int": ("int", ""),
		"Long Int": ("bigint", "20"),
		"Float": ("decimal", "21,9"),
		"Percent": ("decimal", "21,9"),
		"Check": ("tinyint", ""),
		"Small Text": ("text", ""),
		"Long Text": ("text", ""),
		"Code": ("text", ""),
		"Text Editor": ("text", ""),
		"Markdown Editor": ("text", ""),
		"HTML Editor": ("text", ""),
		"Date": ("date", ""),
		"Datetime": ("datetime", ""),
		"Time": ("time", ""),
		"Text": ("text", ""),
		"Data": ("varchar", frappe.db.VARCHAR_LEN),
		"Link": ("varchar", frappe.db.VARCHAR_LEN),
		"Dynamic Link": ("varchar", frappe.db.VARCHAR_LEN),
		"Password": ("text", ""),
		"Select": ("varchar", frappe.db.VARCHAR_LEN),
		"Rating": ("decimal", "3,2"),
		"Read Only": ("varchar", frappe.db.VARCHAR_LEN),
		"Attach": ("text", ""),
		"Attach Image": ("text", ""),
		"Signature": ("text", ""),
		"Color": ("varchar", frappe.db.VARCHAR_LEN),
		"Barcode": ("text", ""),
		"Geolocation": ("text", ""),
		"Duration": ("decimal", "21,9"),
		"Icon": ("varchar", frappe.db.VARCHAR_LEN),
		"Phone": ("varchar", frappe.db.VARCHAR_LEN),
		"Autocomplete": ("varchar", frappe.db.VARCHAR_LEN),
		"JSON": ("json", ""),
	}


def get_pyarrow_type_map():
	import pyarrow as pa

	return {
		"Currency": pa.float64(),
		"Int": pa.int32(),
		"Long Int": pa.int64(),
		"Float": pa.float64(),
		"Percent": pa.float64(),
		"Check": pa.int8(),
		"Small Text": pa.string(),
		"Long Text": pa.string(),
		"Code": pa.string(),
		"Text Editor": pa.string(),
		"Markdown Editor": pa.string(),
		"HTML Editor": pa.string(),
		"Date": pa.date32(),
		"Datetime": pa.timestamp("us"),
		"Time": pa.time64("us"),
		"Text": pa.string(),
		"Data": pa.string(),
		"Link": pa.string(),
		"Dynamic Link": pa.string(),
		"Password": pa.string(),
		"Select": pa.string(),
		"Rating": pa.float64(),
		"Read Only": pa.string(),
		"Attach": pa.string(),
		"Attach Image": pa.string(),
		"Signature": pa.string(),
		"Color": pa.string(),
		"Barcode": pa.string(),
		"Geolocation": pa.string(),
		"Duration": pa.float64(),
		"Icon": pa.string(),
		"Phone": pa.string(),
		"Autocomplete": pa.string(),
		"JSON": pa.large_string(),
	}


def get_latest_sync(doctype: str | None = None, read_only: bool = False):
	"""Return a connection to the most recent *completed* sync of `doctype`, or None.

	A sync is submitted before its rows exist -- `on_submit` only creates the tables and enqueues
	the load, and each table is emptied before it is refilled -- so `docstatus` alone is not a
	completion marker. Serving the newest submitted sync hands a report the file currently being
	loaded, which answers with zeros rather than raising. Only a sync whose items are all synced
	may be served; that is the same condition `is_data_sync_pending` reports on.

	Pass `read_only` for reads. DuckDB allows several processes on one file only while they are
	all read-only, so a read-write handle would make two concurrent reports collide.
	"""
	sync = latest_completed_sync(doctype)
	return get_duckdb(read_only, sync.filename) if sync else None


def latest_completed_sync(doctype: str | None = None):
	"""The most recent completed `DuckDB Sync` for `doctype`, or None. See `get_latest_sync`."""
	if not doctype:
		return None

	sync = frappe.qb.DocType("DuckDB Sync")
	item = frappe.qb.DocType("DuckDB Sync Item")
	loading = frappe.qb.from_(item).select(item.parent).where(item.synced == 0)

	completed = (
		frappe.qb.from_(sync)
		.select(sync.filename, sync.creation)
		.where((sync.doc_type == doctype) & (sync.docstatus == 1) & sync.name.notin(loading))
		.orderby(sync.creation, order=Order.desc)
		.limit(1)
		.run(as_dict=True)
	)

	return completed[0] if completed else None


@contextmanager
def snapshot(doctypes: list[str]):
	"""Route query builder reads of `doctypes` to their latest synced DuckDB files.

	Set by the report runner, not by report code: inside this block a query whose tables are all
	synced executes against DuckDB, and everything else -- `frappe.db`, `frappe.get_all`, and any
	query touching an unsynced doctype -- keeps going to the site database. `frappe.local.db` is
	deliberately not swapped, because the snapshots hold the synced doctypes and nothing else.

	Each synced doctype has its own file, so they are ATTACHed read-only onto one connection and
	put on the search path. That is what lets a query join two synced doctypes: DuckDB resolves
	unqualified table names across attached databases, so the join is served here rather than
	falling back for spanning two files.
	"""
	import duckdb

	previous = getattr(frappe.local, "query_targets", None)

	# One connection with every snapshot ATTACHed read-only, rather than one per doctype: DuckDB
	# resolves unqualified table names across attached databases once they are on the search path,
	# so a query joining two synced doctypes is served here instead of falling back.
	files = {}
	for doctype in doctypes:
		if sync := latest_completed_sync(doctype):
			files[frappe.scrub(doctype)] = duckdb_file_path(sync.filename)

	conn = None
	targets = []

	if files:
		conn = DuckDBConnection(duckdb.connect())
		for alias, path in files.items():
			# ATTACH takes no bind parameters, and both parts are server-generated (a scrubbed
			# doctype and a filename this module built), but quote them anyway
			conn.execute(f"""ATTACH '{path.replace("'", "''")}' AS "{alias}" (READ_ONLY)""")

		conn.execute("SET search_path='{}'".format(",".join(files)))
		apply_duckdb_limits(conn)
		tables = {row[0] for row in conn.sql("show tables").fetchall()}
		targets.append(DuckDBSnapshotTarget(conn, tables))

	frappe.local.query_targets = targets
	try:
		yield targets
	finally:
		frappe.local.query_targets = previous
		if conn:
			conn.close()


class DuckDBConnection:
	"""Wraps a DuckDB connection so fetch results automatically convert Decimal to float."""

	def __init__(self, conn):
		self._conn = conn

	def __getattr__(self, name):
		return getattr(self._conn, name)

	def execute(self, query, parameters=None):
		rel = self._conn.execute(query, parameters) if parameters is not None else self._conn.execute(query)
		return DuckDBRelation(rel)

	def sql(self, query):
		return DuckDBRelation(self._conn.sql(query))


class DuckDBRelation:
	"""Wraps a DuckDB relation to convert Decimal results to float on fetch."""

	def __init__(self, rel):
		self._rel = rel

	def __getattr__(self, name):
		return getattr(self._rel, name)

	def fetchall(self):
		from decimal import Decimal

		return [tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in self._rel.fetchall()]

	def fetchone(self):
		from decimal import Decimal

		row = self._rel.fetchone()
		if row is None:
			return None
		return tuple(float(v) if isinstance(v, Decimal) else v for v in row)

	def fetchmany(self, size=1):
		from decimal import Decimal

		return [
			tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in self._rel.fetchmany(size)
		]


def apply_duckdb_limits(conn, writing: bool = False):
	"""Bound what one DuckDB connection may take from a shared frappe process.

	DuckDB otherwise uses every core and up to ~80% of system memory, and it recommends capping
	both when it shares a machine with other work -- which a gunicorn or RQ worker always does.
	These are per-machine resource limits rather than product settings, so they come from
	`site_config.json` (`duckdb_threads`, `duckdb_memory_limit`) alongside the other tuning knobs,
	and are left at DuckDB's defaults when unset.
	"""
	if threads := frappe.conf.get("duckdb_threads"):
		conn.execute(f"SET threads = {int(threads)}")

	if memory_limit := frappe.conf.get("duckdb_memory_limit"):
		conn.execute("SET memory_limit = '{}'".format(str(memory_limit).replace("'", "")))

	if writing:
		# documented for queries that write a lot of data; the sync does not depend on the order
		# rows land in, and it lets DuckDB keep peak memory down while loading
		conn.execute("SET preserve_insertion_order = false")


def attach_live(doctype: str, filters=None, fields: list[str] | None = None):
	"""Make live rows of `doctype` queryable alongside any open snapshot, else do nothing.

	A snapshot holds only the synced doctypes, so a report joining its fact table to master data
	falls back to the site database and gets no acceleration -- which is most reports. Calling
	this before such a query keeps the join in DuckDB, with the dimension read live so it is not
	stale. It is a no-op when no snapshot is open, so callers need no branch of their own.

	`filters` bound what is materialised; an unbounded master table would land in memory in full.
	"""
	for target in getattr(frappe.local, "query_targets", None) or ():
		target.attach_live(doctype, filters=filters, fields=fields)


def doctypes_to_sync() -> list[str]:
	"""Doctypes replicated to DuckDB, declared once in System Settings.

	A report only says *that* it is a snapshot report; which doctypes are replicated is a
	site-level operational choice (it depends on data volume), so it is not repeated per report.
	"""
	return frappe.get_all(
		"Doctype To Sync",
		filters={"parenttype": "System Settings", "parentfield": "doctype_to_sync"},
		pluck="doc_type",
		distinct=True,
	)


def snapshot_taken_at():
	"""When the data a snapshot report reads was captured, or None if nothing is ready.

	A report reads every synced doctype, so it is only as fresh as the *stalest* of them.
	"""
	times = [sync.creation for dt in doctypes_to_sync() if (sync := latest_completed_sync(dt))]
	return min(times) if times else None


def start_duckdb_sync():
	for doctype in doctypes_to_sync():
		frappe.get_doc({"doctype": "DuckDB Sync", "doc_type": doctype}).insert().submit()


class DuckDBSnapshotTarget:
	"""The synced snapshots, offered to the query builder as a place to read from.

	This is the whole of what `frappe.query_builder` knows about DuckDB: it asks `can_serve`
	whether a query's tables are here, renders for `dialect`, hands the SQL to `execute`, and
	treats `fallback_errors` as "this target cannot answer, use the site database".
	"""

	dialect = db_type_is.DUCKDB

	def __init__(self, conn, tables: set[str]):
		self.conn = conn
		self.tables = tables
		# what the attached snapshot files hold, as opposed to anything registered live later
		self.snapshot_tables = set(tables)
		# doctypes `recover` has already tried, so a repeated failure gives up instead of looping
		self.attempted: set[str] = set()

	@property
	def fallback_errors(self):
		"""Any DuckDB failure sends the query back to the site database.

		This is an accelerator, not the source of truth, so it must never turn a query the site
		database can answer into a failed report. Callers bake dialect decisions in at build time
		-- erpnext alone branches on `db_type` in 31 places, one of which emits mariadb's
		'0000-00-00' zero date -- and those reach DuckDB as conversion, parser or binder errors
		alike. Narrowing this to a few exception types just means the ones left out crash.
		"""
		# only evaluated when a query actually raises, so the import costs nothing otherwise
		import duckdb

		return (duckdb.Error,)

	def attach_live(self, doctype: str, filters=None, fields: list[str] | None = None):
		"""Expose live rows of `doctype` to the snapshot connection as an Arrow table.

		A snapshot holds only the synced doctypes, so a query joining one to master data falls
		back to the site database and loses the acceleration entirely. Registering the rows that
		query needs keeps the join inside DuckDB while the master data stays current -- the
		snapshot supplies the frozen facts, this supplies the live dimensions.

		`filters` bound what is materialised: the caller knows the scope its query will read, and
		an unbounded master table would land in memory in full. Registering is idempotent, so a
		second call with a wider scope replaces the first.

		A doctype that is itself synced is left alone. A registered table *shadows* an attached
		one of the same name, so registering over a snapshot would silently swap frozen rows for
		live ones for every query in the block -- and the snapshot copy is both consistent with
		the rest of the report and faster to scan.
		"""
		import pyarrow as pa

		if get_table_name(doctype) in self.snapshot_tables:
			return

		# the arrow schema describes the table as the sync would create it, which can name columns
		# an unsynced doctype does not actually have; get_valid_columns is what really exists
		schema = DuckDBTable(doctype).get_arrow_schema()
		fields = fields or frappe.get_meta(doctype).get_valid_columns()
		schema = pa.schema([schema.field(name) for name in fields if name in schema.names])

		rows = frappe.get_all(doctype, filters=filters, fields=list(schema.names), limit_page_length=0)

		# mariadb hands a Time field back as a timedelta, which has no Arrow type; the sync
		# converts the same way on its way in
		times = [
			name for name, dtype in zip(schema.names, schema.types, strict=False) if pa.types.is_time(dtype)
		]
		for row in rows:
			for name in times:
				if isinstance(value := row.get(name), timedelta):
					row[name] = (datetime.min + value).time()

		table = get_table_name(doctype)
		# an explicit schema keeps the column types when there are no matching rows, so a join
		# against an empty dimension still resolves instead of failing to bind
		self.conn.register(table, pa.Table.from_pylist(rows, schema=schema))
		self.tables.add(table)

	def recover(self, error) -> bool:
		"""Supply a table the query needed and this target did not have, if that is safe.

		The structural check in `get_query_target` only sees `_from` and `_joins`; a dimension
		reached through a subquery (`ExistsCriterion` hides what it wraps, `RawCriterion` is
		opaque SQL) is invisible until DuckDB binds the query and says what is missing. Reading
		that dimension live is what keeps the join here instead of sending the whole query to the
		site database, and it is bounded by `duckdb_live_table_limit` so an unexpectedly large
		table falls back rather than landing in memory.

		Returns whether anything changed, i.e. whether retrying is worth it.
		"""
		match = MISSING_TABLE.search(str(error))
		if not match:
			return False

		doctype = get_doctype_name(match.group(1))
		if doctype in self.attempted or not frappe.db.exists("DocType", doctype):
			return False

		self.attempted.add(doctype)

		limit = frappe.conf.get("duckdb_live_table_limit", 100_000)
		if frappe.db.count(doctype) > limit:
			return False

		self.attach_live(doctype)
		return True

	def can_serve(self, tables: set[str]) -> bool:
		"""The snapshots hold the synced doctypes, so anything else falls back."""
		return tables <= self.tables

	def execute(self, sql, params, columns=None, **kwargs):
		return execute_snapshot_query(self.conn, sql, params, columns, **kwargs)

	def close(self):
		self.conn.close()


def restore_snapshot_types(rows, description):
	"""Give Time values back the type the site database would have returned.

	MariaDB hands a Time field back as a `timedelta`, but pyarrow has no duration-of-day type, so
	`sync_using_pyarrow` converts it to a `datetime.time` on the way in. Reading it straight back
	would make a snapshot answer with a different type than the same query on the site database
	-- visible in erpnext's stock ledger, whose rows carry `posting_time`.
	"""
	if frappe.local.db.db_type != "mariadb":
		return rows

	columns = tuple(index for index, column in enumerate(description) if str(column[1]) == "TIME")
	if not columns:
		return rows

	restored = []
	for row in rows:
		row = list(row)
		for index in columns:
			if isinstance(value := row[index], time):
				row[index] = timedelta(
					hours=value.hour,
					minutes=value.minute,
					seconds=value.second,
					microseconds=value.microsecond,
				)
		restored.append(tuple(row))

	return restored


def execute_snapshot_query(conn, sql, params, columns=None, **kwargs):
	"""Run a rendered snapshot query, shaped the way `Database.sql` would shape it.

	`Database.sql` takes everything after `values` keyword-only, so there are no positional
	arguments to forward here.
	"""
	if not kwargs.get("run", True):
		return sql

	if kwargs.get("debug"):
		frappe.log(sql)

	relation = conn.execute(sql, params or None)
	if not relation.description:
		return ()

	# the names computed from the query only apply if they line up with what came back -- a
	# query whose select list is not one-to-one with its result columns falls back to the driver
	if not columns or len(columns) != len(relation.description):
		columns = [column[0] for column in relation.description]

	if kwargs.get("as_iterator"):
		# erpnext's accounts receivable/payable streams its ledger query for large companies
		return _stream_snapshot_result(relation, columns, kwargs)

	result = restore_snapshot_types(relation.fetchall(), relation.description)

	if kwargs.get("pluck"):
		return [row[0] for row in result]

	if kwargs.get("as_dict"):
		result = [frappe._dict(zip(columns, row, strict=False)) for row in result]
		if update := kwargs.get("update"):
			for row in result:
				row.update(update)
		return result

	if kwargs.get("as_list"):
		return [list(row) for row in result]

	# `Database.sql` returns whatever the driver's fetchall() gives, which on mariadb is a tuple
	# of tuples -- a snapshot report comparing or concatenating its result must not get a list
	# where the same query on the site database gives a tuple.
	return tuple(result)


def _stream_snapshot_result(relation, columns, kwargs):
	"""Stream a snapshot result, mirroring `Database._return_as_iterator`."""
	from frappe.database.database import SQL_ITERATOR_BATCH_SIZE

	pluck, as_dict, as_list = kwargs.get("pluck"), kwargs.get("as_dict"), kwargs.get("as_list")
	update = kwargs.get("update")

	while batch := relation.fetchmany(SQL_ITERATOR_BATCH_SIZE):
		result = restore_snapshot_types(batch, relation.description)
		if pluck:
			for row in result:
				yield row[0]
		elif as_dict:
			for row in result:
				row = frappe._dict(zip(columns, row, strict=False))
				if update:
					row.update(update)
				yield row
		elif as_list:
			for row in result:
				yield list(row)
		else:
			from frappe import _, throw

			throw(_("`as_iterator` only works with `as_list=True` or `as_dict=True`"))
