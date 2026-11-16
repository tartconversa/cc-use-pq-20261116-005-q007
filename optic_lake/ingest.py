import json
from decimal import Decimal

from .catalog import Catalog
from .errors import Conflict, Invalid, Missing, check
from .storage import Storage
from .values import canonical, decstr, fingerprint, iso_day, json_row, key, number, obj, seq, utc


UNIT_FACTORS = {'mm': Decimal('1000'), 'um': Decimal('1'), 'nm': Decimal('0.001')}


class Ingestor:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.catalog = Catalog(storage)

    def submit(self, batch_id, source_id, source_key, submitted_at, rows):
        batch_id = key(batch_id, 'batch_id')
        source_id = key(source_id, 'source_id')
        source_key = key(source_key, 'source_key')
        submitted_at = utc(submitted_at, 'submitted_at')
        rows = seq(rows, 'rows', 1000)
        for raw in rows:
            obj(raw, 'raw row')
            check(len(canonical(raw)) <= 16000, 'raw row is too large')
        digest = fingerprint({'source_id': source_id, 'rows': rows})
        with self.storage.write() as db:
            source = self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?',
                                      (source_id,), 'source')
            check(bool(source['enabled']), 'source disabled')
            prior = db.execute('SELECT * FROM raw_batches WHERE source_id=? AND source_key=?',
                               (source_id, source_key)).fetchone()
            if prior:
                if prior['payload_digest'] != digest or prior['batch_id'] != batch_id:
                    raise Conflict('source key was used for another batch payload')
                return json_row(prior['result_json'])
            if db.execute('SELECT 1 FROM raw_batches WHERE batch_id=?', (batch_id,)).fetchone():
                raise Conflict('batch_id already exists')
            results = []
            staged = set()
            normalized = []
            for ordinal, raw in enumerate(rows):
                try:
                    event, contract = self._normalize(db, source, raw)
                    event_key = event['external_id']
                    if event_key in staged:
                        raise Conflict('external event repeated within batch')
                    staged.add(event_key)
                    existing = db.execute('SELECT * FROM accepted_events WHERE source_id=? AND external_id=?',
                                          (source_id, event_key)).fetchone()
                    if existing:
                        if existing['payload_digest'] != fingerprint(event):
                            raise Conflict('external event identity has different content')
                        results.append({'ordinal': ordinal, 'outcome': 'duplicate',
                                        'external_id': event_key, 'reason': None})
                        normalized.append((None, contract['contract_id']))
                    else:
                        results.append({'ordinal': ordinal, 'outcome': 'accepted',
                                        'external_id': event_key, 'reason': None})
                        normalized.append((event, contract['contract_id']))
                except (Invalid, Conflict, Missing) as exc:
                    results.append({'ordinal': ordinal, 'outcome': 'quarantined',
                                    'external_id': self._safe_external(raw), 'reason': str(exc)})
                    normalized.append((None, None))
            accepted = sum(r['outcome'] == 'accepted' for r in results)
            duplicates = sum(r['outcome'] == 'duplicate' for r in results)
            quarantined = len(rows) - accepted - duplicates
            result = {'batch_id': batch_id, 'source_id': source_id, 'source_key': source_key,
                      'accepted': accepted, 'duplicate': duplicates, 'quarantined': quarantined,
                      'rows': results}
            # The raw batch is durable even when every row is rejected.
            batch_revision = self.storage.advance(db, 'raw-batch', batch_id)
            contract_ids = [cid for _, cid in normalized if cid]
            batch_contract = contract_ids[0] if contract_ids else self._fallback_contract(db, source_id)
            db.execute('INSERT INTO raw_batches VALUES (?,?,?,?,?,?,?,?)',
                       (batch_id, source_id, source_key, digest, batch_contract, submitted_at,
                        canonical(result), batch_revision))
            for ordinal, (raw, item, pair) in enumerate(zip(rows, results, normalized)):
                event, _ = pair
                revision = None
                if event is not None:
                    revision = self.storage.advance(db, 'event', f'{source_id}:{event["external_id"]}')
                    self._insert_event(db, source_id, source, batch_id, ordinal, event, revision,
                                       pair[1])
                db.execute('INSERT INTO raw_rows VALUES (?,?,?,?,?,?,?,?)',
                           (batch_id, ordinal, item['external_id'], canonical(raw), fingerprint(raw),
                            item['outcome'], item['reason'], revision))
            result['source_revision'] = self.storage.revision(db)
            db.execute('UPDATE raw_batches SET result_json=? WHERE batch_id=?',
                       (canonical(result), batch_id))
        return result

    def _fallback_contract(self, db, source_id):
        row = db.execute('SELECT contract_id FROM contracts WHERE source_id=? ORDER BY effective_day LIMIT 1',
                         (source_id,)).fetchone()
        if row is None:
            raise Invalid('source requires a contract before ingestion')
        return row[0]

    @staticmethod
    def _safe_external(raw):
        value = raw.get('external_id')
        return value if isinstance(value, str) and len(value) <= 80 else None

    def _normalize(self, db, source, raw):
        raw_time = raw.get('occurred_at')
        if raw_time is None:
            # A contract can rename the time field. Search each contract's mapping.
            candidates = db.execute('SELECT * FROM contracts WHERE source_id=?',
                                    (source['source_id'],)).fetchall()
            matches = []
            for item in candidates:
                mapped = json.loads(item['field_map_json'])['occurred_at']
                if mapped in raw:
                    try:
                        day = utc(raw[mapped])[:10]
                    except Invalid:
                        continue
                    if item['effective_day'] <= day and (item['retired_day'] is None or day < item['retired_day']):
                        matches.append((item, day))
            if len(matches) != 1:
                raise Invalid('cannot resolve contract from event time')
            contract, day = matches[0]
        else:
            day = utc(raw_time)[:10]
            contract = self.catalog.resolve_contract(db, source['source_id'], day)
        mapping = json.loads(contract['field_map_json'])
        required = json.loads(contract['required_json'])
        for field in required:
            if mapping[field] not in raw or raw[mapping[field]] is None:
                raise Invalid(f'missing required field {field}')
        permitted = set(mapping.values())
        if set(raw) - permitted:
            raise Invalid('raw row has unmapped fields')
        data = {field: raw.get(source_name) for field, source_name in mapping.items()}
        occurred_at = utc(data['occurred_at'], 'occurred_at')
        if occurred_at[:10] != day:
            raise Invalid('event time differs from contract day')
        if not contract['effective_day'] <= day or (contract['retired_day'] and day >= contract['retired_day']):
            raise Invalid('contract not effective for event day')
        family = self.storage.get(db, 'SELECT * FROM product_families WHERE family_id=?',
                                  (key(data['family_id'], 'family_id'),), 'family')
        station = self.storage.get(db, 'SELECT * FROM stations WHERE station_id=?',
                                   (key(data['station_id'], 'station_id'),), 'station')
        if family['facility_id'] != source['facility_id'] or station['facility_id'] != source['facility_id']:
            raise Invalid('family or station belongs to another facility')
        if station['stage'] != source['stage']:
            raise Invalid('station belongs to another stage')
        if day < family['active_from'] or family['active_to'] and day >= family['active_to']:
            raise Invalid('family inactive at event time')
        if day < station['active_from'] or station['active_to'] and day >= station['active_to']:
            raise Invalid('station inactive at event time')
        facility = self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                                    (source['facility_id'],), 'facility')
        if day < facility['opened_day'] or facility['closed_day'] and day >= facility['closed_day']:
            raise Invalid('facility inactive at event time')
        units = number(data['units'], 'units', positive=True)
        passed = number(data['passed_units'], 'passed_units', nonnegative=True)
        rejected = number(data['rejected_units'], 'rejected_units', nonnegative=True)
        if units != passed + rejected:
            raise Invalid('units must equal passed plus rejected')
        if any(quantity != quantity.to_integral_value() for quantity in (units, passed, rejected)):
            raise Invalid('quantities must be whole lens units')
        measurement, measurement_unit = self._measurement(data, json.loads(contract['unit_map_json']))
        event = {'external_id': key(data['external_id'], 'external_id'),
                 'family_id': family['family_id'], 'lot_id': key(data['lot_id'], 'lot_id'),
                 'station_id': station['station_id'], 'occurred_at': occurred_at,
                 'event_day': day, 'units': decstr(units), 'passed_units': decstr(passed),
                 'rejected_units': decstr(rejected), 'measured_value': measurement,
                 'measured_unit': measurement_unit, 'stage': source['stage'],
                 'facility_id': source['facility_id']}
        lot = db.execute('SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                         (source['facility_id'], event['lot_id'])).fetchone()
        if lot and lot['family_id'] != family['family_id']:
            raise Invalid('lot belongs to another family')
        if lot and day < lot['created_day']:
            raise Invalid('event predates lot creation')
        return event, contract

    @staticmethod
    def _measurement(data, unit_map):
        value, unit = data['measured_value'], data['measured_unit']
        if value is None and unit is None:
            return None, None
        if value is None or unit is None:
            raise Invalid('measurement value and unit must be paired')
        if unit not in unit_map or unit not in UNIT_FACTORS:
            raise Invalid('measurement unit is not mapped')
        measured = number(value, 'measured_value', nonnegative=True) * UNIT_FACTORS[unit]
        return decstr(measured), unit_map[unit]

    def _insert_event(self, db, source_id, source, batch_id, ordinal, event, revision, contract_id):
        if not db.execute('SELECT 1 FROM production_lots WHERE facility_id=? AND lot_id=?',
                          (source['facility_id'], event['lot_id'])).fetchone():
            db.execute('INSERT INTO production_lots VALUES (?,?,?,?,?)',
                       (source['facility_id'], event['lot_id'], event['family_id'],
                        event['event_day'], revision))
        db.execute('INSERT INTO accepted_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (source_id, event['external_id'], event['stage'], event['facility_id'],
                    event['family_id'], event['lot_id'], event['station_id'], event['occurred_at'],
                    event['event_day'], event['units'], event['passed_units'], event['rejected_units'],
                    event['measured_value'], event['measured_unit'], contract_id, batch_id,
                    ordinal, fingerprint(event), revision))

    def batch(self, batch_id):
        batch_id = key(batch_id, 'batch_id')
        with self.storage.read() as db:
            batch = self.storage.get(db, 'SELECT * FROM raw_batches WHERE batch_id=?',
                                     (batch_id,), 'batch')
            result = json_row(batch['result_json'])
            result['contract_id'] = batch['contract_id']
            result['submitted_at'] = batch['submitted_at']
            return result

    def quarantine(self, batch_id):
        batch_id = key(batch_id, 'batch_id')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM raw_batches WHERE batch_id=?', (batch_id,), 'batch')
            return [{'ordinal': r['ordinal'], 'external_id': r['external_id'],
                     'reason': r['reason'], 'raw': json_row(r['raw_json'])}
                    for r in db.execute('SELECT * FROM raw_rows WHERE batch_id=? AND outcome=? ORDER BY ordinal',
                                        (batch_id, 'quarantined'))]

    def event(self, source_id, external_id):
        source_id, external_id = key(source_id, 'source_id'), key(external_id, 'external_id')
        with self.storage.read() as db:
            row = self.storage.get(db, 'SELECT * FROM accepted_events WHERE source_id=? AND external_id=?',
                                   (source_id, external_id), 'event')
            return dict(row)
