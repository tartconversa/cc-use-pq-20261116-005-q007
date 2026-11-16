import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import Missing


DDL = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS clock (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
  published_head INTEGER NOT NULL
);
INSERT OR IGNORE INTO clock VALUES (1,0,0);
CREATE TABLE IF NOT EXISTS audit (
  revision INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS facilities (
  facility_id TEXT PRIMARY KEY, name TEXT NOT NULL, opened_day TEXT NOT NULL,
  closed_day TEXT, created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS product_families (
  family_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  name TEXT NOT NULL, active_from TEXT NOT NULL, active_to TEXT,
  created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
  station_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  stage TEXT NOT NULL, active_from TEXT NOT NULL, active_to TEXT,
  created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  stage TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
  created_revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
  contract_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  version INTEGER NOT NULL, effective_day TEXT NOT NULL, retired_day TEXT,
  field_map_json TEXT NOT NULL, unit_map_json TEXT NOT NULL,
  required_json TEXT NOT NULL, digest TEXT NOT NULL, created_revision INTEGER NOT NULL,
  UNIQUE(source_id,version)
);
CREATE INDEX IF NOT EXISTS contracts_date ON contracts(source_id,effective_day,retired_day);
CREATE TABLE IF NOT EXISTS raw_batches (
  batch_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
  source_key TEXT NOT NULL, payload_digest TEXT NOT NULL, contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
  submitted_at TEXT NOT NULL, result_json TEXT NOT NULL, source_revision INTEGER NOT NULL,
  UNIQUE(source_id,source_key)
);
CREATE TABLE IF NOT EXISTS raw_rows (
  batch_id TEXT NOT NULL REFERENCES raw_batches(batch_id), ordinal INTEGER NOT NULL,
  external_id TEXT, raw_json TEXT NOT NULL, raw_digest TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('accepted','duplicate','quarantined')),
  reason TEXT, event_revision INTEGER, PRIMARY KEY(batch_id,ordinal)
);
CREATE TABLE IF NOT EXISTS accepted_events (
  source_id TEXT NOT NULL REFERENCES sources(source_id), external_id TEXT NOT NULL,
  stage TEXT NOT NULL, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  family_id TEXT NOT NULL REFERENCES product_families(family_id),
  lot_id TEXT NOT NULL, station_id TEXT NOT NULL REFERENCES stations(station_id),
  occurred_at TEXT NOT NULL, event_day TEXT NOT NULL, units TEXT NOT NULL,
  passed_units TEXT NOT NULL, rejected_units TEXT NOT NULL,
  measured_value TEXT, measured_unit TEXT, contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
  batch_id TEXT NOT NULL REFERENCES raw_batches(batch_id), ordinal INTEGER NOT NULL,
  payload_digest TEXT NOT NULL, revision INTEGER NOT NULL,
  PRIMARY KEY(source_id,external_id), UNIQUE(batch_id,ordinal)
);
CREATE INDEX IF NOT EXISTS events_lot ON accepted_events(facility_id,lot_id,stage,event_day);
CREATE INDEX IF NOT EXISTS events_revision ON accepted_events(revision);
CREATE TABLE IF NOT EXISTS production_lots (
  facility_id TEXT NOT NULL REFERENCES facilities(facility_id), lot_id TEXT NOT NULL,
  family_id TEXT NOT NULL REFERENCES product_families(family_id), created_day TEXT NOT NULL,
  created_revision INTEGER NOT NULL, PRIMARY KEY(facility_id,lot_id)
);
CREATE TABLE IF NOT EXISTS material_links (
  link_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  parent_lot TEXT NOT NULL, child_lot TEXT NOT NULL, used_units TEXT NOT NULL,
  created_revision INTEGER NOT NULL,
  FOREIGN KEY(facility_id,parent_lot) REFERENCES production_lots(facility_id,lot_id),
  FOREIGN KEY(facility_id,child_lot) REFERENCES production_lots(facility_id,lot_id)
);
CREATE INDEX IF NOT EXISTS links_child ON material_links(facility_id,child_lot);
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id TEXT PRIMARY KEY, facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
  from_day TEXT NOT NULL, to_day TEXT NOT NULL, source_revision INTEGER NOT NULL,
  expected_head INTEGER NOT NULL, rows_json TEXT NOT NULL, rows_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('prepared','published'))
);
CREATE TABLE IF NOT EXISTS releases (
  version INTEGER PRIMARY KEY, snapshot_id TEXT UNIQUE NOT NULL REFERENCES snapshots(snapshot_id),
  facility_id TEXT NOT NULL, from_day TEXT NOT NULL, to_day TEXT NOT NULL,
  source_revision INTEGER NOT NULL, rows_digest TEXT NOT NULL,
  published_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS release_rows (
  version INTEGER NOT NULL REFERENCES releases(version), row_key TEXT NOT NULL,
  data_json TEXT NOT NULL, PRIMARY KEY(version,row_key)
);
"""


class Storage:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.executescript(DDL)
            if self.path != ":memory:":
                db.execute("PRAGMA journal_mode=WAL")
        finally:
            db.close()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        return db

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        db = self.connect()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def revision(db):
        return db.execute("SELECT revision FROM clock WHERE singleton=1").fetchone()[0]

    @staticmethod
    def head(db):
        return db.execute("SELECT published_head FROM clock WHERE singleton=1").fetchone()[0]

    @staticmethod
    def advance(db, kind, identity):
        db.execute("UPDATE clock SET revision=revision+1 WHERE singleton=1")
        revision = Storage.revision(db)
        db.execute("INSERT INTO audit(revision,kind,identity) VALUES (?,?,?)", (revision,kind,identity))
        return revision

    @staticmethod
    def get(db, query, args, name):
        row = db.execute(query,args).fetchone()
        if row is None:
            raise Missing(f"{name} not found")
        return row

    def state(self):
        with self.read() as db:
            return {"source_revision": self.revision(db), "published_head": self.head(db),
                    "facilities": db.execute("SELECT count(*) FROM facilities").fetchone()[0],
                    "events": db.execute("SELECT count(*) FROM accepted_events").fetchone()[0]}
