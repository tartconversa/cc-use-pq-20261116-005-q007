import json

from .errors import Conflict, Invalid, check
from .storage import Storage
from .values import canonical, fingerprint, iso_day, key, label, obj, seq, stage


FIELDS = ('external_id', 'family_id', 'lot_id', 'station_id', 'occurred_at',
          'units', 'passed_units', 'rejected_units', 'measured_value', 'measured_unit')
REQUIRED = set(FIELDS[:8])


class Catalog:
    def __init__(self, storage: Storage):
        self.storage = storage

    def facility(self, facility_id, name, opened_day, closed_day=None):
        facility_id, name = key(facility_id, 'facility_id'), label(name, 'name')
        opened_day, closed_day = self.interval(opened_day, closed_day)
        with self.storage.write() as db:
            self.unique(db, 'facilities', 'facility_id', facility_id)
            revision = self.storage.advance(db, 'facility', facility_id)
            db.execute('INSERT INTO facilities VALUES (?,?,?,?,?)',
                       (facility_id, name, opened_day, closed_day, revision))
        return {'facility_id': facility_id, 'revision': revision}

    def family(self, family_id, facility_id, name, active_from, active_to=None):
        family_id, facility_id = key(family_id, 'family_id'), key(facility_id, 'facility_id')
        name = label(name, 'name')
        active_from, active_to = self.interval(active_from, active_to)
        with self.storage.write() as db:
            self.facility_window(db, facility_id, active_from, active_to)
            self.unique(db, 'product_families', 'family_id', family_id)
            revision = self.storage.advance(db, 'family', family_id)
            db.execute('INSERT INTO product_families VALUES (?,?,?,?,?,?)',
                       (family_id, facility_id, name, active_from, active_to, revision))
        return {'family_id': family_id, 'revision': revision}

    def station(self, station_id, facility_id, process_stage, active_from, active_to=None):
        station_id, facility_id = key(station_id, 'station_id'), key(facility_id, 'facility_id')
        process_stage = stage(process_stage)
        active_from, active_to = self.interval(active_from, active_to)
        with self.storage.write() as db:
            self.facility_window(db, facility_id, active_from, active_to)
            self.unique(db, 'stations', 'station_id', station_id)
            revision = self.storage.advance(db, 'station', station_id)
            db.execute('INSERT INTO stations VALUES (?,?,?,?,?,?)',
                       (station_id, facility_id, process_stage, active_from, active_to, revision))
        return {'station_id': station_id, 'revision': revision}

    def source(self, source_id, facility_id, process_stage):
        source_id, facility_id = key(source_id, 'source_id'), key(facility_id, 'facility_id')
        process_stage = stage(process_stage)
        with self.storage.write() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?', (facility_id,), 'facility')
            self.unique(db, 'sources', 'source_id', source_id)
            revision = self.storage.advance(db, 'source', source_id)
            db.execute('INSERT INTO sources VALUES (?,?,?,?,?)',
                       (source_id, facility_id, process_stage, 1, revision))
        return {'source_id': source_id, 'revision': revision}

    def enable_source(self, source_id, enabled):
        source_id = key(source_id, 'source_id')
        check(type(enabled) is bool, 'enabled must be boolean')
        with self.storage.write() as db:
            source = self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?',
                                      (source_id,), 'source')
            if bool(source['enabled']) == enabled:
                revision = self.storage.revision(db)
            else:
                revision = self.storage.advance(db, 'source-status', source_id)
                db.execute('UPDATE sources SET enabled=? WHERE source_id=?', (int(enabled), source_id))
        return {'source_id': source_id, 'enabled': enabled, 'revision': revision}

    def contract(self, contract_id, source_id, version, effective_day, retired_day,
                 field_map, unit_map, required):
        contract_id, source_id = key(contract_id, 'contract_id'), key(source_id, 'source_id')
        check(type(version) is int and version > 0, 'version must be positive integer')
        effective_day, retired_day = self.interval(effective_day, retired_day)
        field_map, unit_map = obj(field_map, 'field_map'), obj(unit_map, 'unit_map')
        check(set(field_map) == set(FIELDS), 'field map must cover canonical fields')
        check(all(isinstance(v, str) and v for v in field_map.values()), 'field names must be text')
        check(len(set(field_map.values())) == len(field_map), 'field names must be distinct')
        check(set(unit_map) <= {'mm', 'um', 'nm'}, 'unsupported source measurement unit')
        check(all(v == 'um' for v in unit_map.values()), 'measurements normalize to um')
        required = seq(required, 'required', 10)
        check(len(required) == len(set(required)) and set(required) >= REQUIRED
              and set(required) <= set(FIELDS), 'invalid required fields')
        payload = {'field_map': field_map, 'unit_map': unit_map, 'required': required}
        with self.storage.write() as db:
            source = self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?',
                                      (source_id,), 'source')
            self.facility_window(db, source['facility_id'], effective_day, retired_day)
            self.unique(db, 'contracts', 'contract_id', contract_id)
            for prior in db.execute('SELECT * FROM contracts WHERE source_id=?', (source_id,)):
                if (retired_day is None or prior['effective_day'] < retired_day) and (
                        prior['retired_day'] is None or effective_day < prior['retired_day']):
                    raise Conflict('contract windows overlap')
            revision = self.storage.advance(db, 'contract', contract_id)
            digest = fingerprint(payload)
            db.execute('INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (contract_id, source_id, version, effective_day, retired_day,
                        canonical(field_map), canonical(unit_map), canonical(required), digest, revision))
        return {'contract_id': contract_id, 'digest': digest, 'revision': revision}

    def resolve_contract(self, db, source_id, day):
        rows = db.execute('SELECT * FROM contracts WHERE source_id=? AND effective_day<=? '
                          'AND (retired_day IS NULL OR retired_day>?)', (source_id, day, day)).fetchall()
        if len(rows) != 1:
            raise Invalid('exactly one effective contract required', source_id=source_id, day=day)
        return rows[0]

    def list_contracts(self, source_id):
        source_id = key(source_id, 'source_id')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?', (source_id,), 'source')
            return [dict(r) for r in db.execute('SELECT * FROM contracts WHERE source_id=? ORDER BY effective_day',
                                                (source_id,))]

    @staticmethod
    def interval(start, end):
        start = iso_day(start, 'start')
        if end is not None:
            end = iso_day(end, 'end')
            check(end > start, 'end must follow start')
        return start, end

    def facility_window(self, db, facility_id, start, end):
        facility = self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                                    (facility_id,), 'facility')
        check(start >= facility['opened_day'], 'window predates facility')
        if facility['closed_day'] is not None:
            check(end is not None and end <= facility['closed_day'], 'window exceeds facility closure')
        return facility

    @staticmethod
    def unique(db, table, column, value):
        if db.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (value,)).fetchone():
            raise Conflict(f'{column} already exists')
