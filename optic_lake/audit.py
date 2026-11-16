from collections import defaultdict
from decimal import Decimal

from .errors import Invalid, check
from .storage import Storage
from .values import decstr, iso_day, json_row, key


class Audit:
    def __init__(self, storage: Storage):
        self.storage = storage

    def timeline(self, from_revision=1, to_revision=None, limit=500):
        check(type(from_revision) is int and from_revision > 0, 'from_revision must be positive')
        check(type(limit) is int and 1 <= limit <= 1000, 'limit must be between 1 and 1000')
        with self.storage.read() as db:
            maximum = self.storage.revision(db)
            to_revision = maximum if to_revision is None else to_revision
            check(type(to_revision) is int and from_revision <= to_revision <= maximum,
                  'invalid revision interval')
            rows = [dict(r) for r in db.execute(
                'SELECT * FROM audit WHERE revision BETWEEN ? AND ? ORDER BY revision LIMIT ?',
                (from_revision, to_revision, limit))]
            return {'from_revision': from_revision, 'to_revision': to_revision,
                    'current_revision': maximum, 'truncated': len(rows) == limit and
                    rows[-1]['revision'] < to_revision, 'records': rows}

    def event_history(self, facility_id, lot_id):
        facility_id, lot_id = key(facility_id, 'facility_id'), key(lot_id, 'lot_id')
        with self.storage.read() as db:
            lot = self.storage.get(db, 'SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                                   (facility_id, lot_id), 'lot')
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND lot_id=? '
                'ORDER BY occurred_at,source_id,external_id', (facility_id, lot_id))]
            linked = [dict(r) for r in db.execute(
                'SELECT * FROM material_links WHERE facility_id=? AND '
                '(parent_lot=? OR child_lot=?) ORDER BY created_revision,link_id',
                (facility_id, lot_id, lot_id))]
            return {'lot': dict(lot), 'events': events, 'links': linked}

    def batch_history(self, source_id, from_day, to_day):
        source_id = key(source_id, 'source_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid batch window')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?', (source_id,), 'source')
            batches = []
            for item in db.execute('SELECT * FROM raw_batches WHERE source_id=? '
                                   'AND substr(submitted_at,1,10) BETWEEN ? AND ? '
                                   'ORDER BY submitted_at,batch_id', (source_id, from_day, to_day)):
                outcome = json_row(item['result_json'])
                counts = {'accepted': 0, 'duplicate': 0, 'quarantined': 0}
                for row in db.execute('SELECT outcome FROM raw_rows WHERE batch_id=?',
                                      (item['batch_id'],)):
                    counts[row['outcome']] += 1
                if sum(counts.values()) != len(outcome['rows']):
                    raise Invalid('stored batch outcome count mismatch', batch_id=item['batch_id'])
                batches.append({'batch_id': item['batch_id'], 'source_key': item['source_key'],
                                'submitted_at': item['submitted_at'], 'contract_id': item['contract_id'],
                                'source_revision': item['source_revision'], 'counts': counts,
                                'payload_digest': item['payload_digest']})
            return {'source_id': source_id, 'from_day': from_day, 'to_day': to_day,
                    'batch_count': len(batches), 'batches': batches}

    def compare_releases(self, left_version, right_version):
        check(type(left_version) is int and left_version > 0, 'left version must be positive')
        check(type(right_version) is int and right_version > 0, 'right version must be positive')
        with self.storage.read() as db:
            left = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                    (left_version,), 'left release')
            right = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                     (right_version,), 'right release')
            left_rows = {r['row_key']: json_row(r['data_json']) for r in db.execute(
                'SELECT * FROM release_rows WHERE version=?', (left_version,))}
            right_rows = {r['row_key']: json_row(r['data_json']) for r in db.execute(
                'SELECT * FROM release_rows WHERE version=?', (right_version,))}
        common = sorted(set(left_rows) & set(right_rows))
        changed = []
        quantity_fields = ('units', 'passed_units', 'rejected_units')
        for row_key in common:
            before, after = left_rows[row_key], right_rows[row_key]
            if before == after:
                continue
            delta = {field: decstr(Decimal(after[field]) - Decimal(before[field]))
                     for field in quantity_fields}
            changed.append({'row_key': row_key, 'before': before, 'after': after,
                            'quantity_delta': delta})
        return {'left_version': left_version, 'right_version': right_version,
                'left_digest': left['rows_digest'], 'right_digest': right['rows_digest'],
                'left_source_revision': left['source_revision'],
                'right_source_revision': right['source_revision'],
                'added': [right_rows[key] for key in sorted(set(right_rows) - set(left_rows))],
                'removed': [left_rows[key] for key in sorted(set(left_rows) - set(right_rows))],
                'changed': changed, 'unchanged_count': len(common) - len(changed)}

    def verify_release(self, version):
        check(type(version) is int and version > 0, 'version must be positive')
        with self.storage.read() as db:
            release = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                       (version,), 'release')
            snapshot = self.storage.get(db, 'SELECT * FROM snapshots WHERE snapshot_id=?',
                                        (release['snapshot_id'],), 'snapshot')
            payload = json_row(snapshot['rows_json'])
            from .values import fingerprint
            digest_matches = fingerprint(payload) == release['rows_digest'] == snapshot['rows_digest']
            materialized = {r['row_key']: json_row(r['data_json']) for r in db.execute(
                'SELECT * FROM release_rows WHERE version=?', (version,))}
            expected = {r['row_key']: r for r in payload['rows']}
            rows_match = materialized == expected
            metadata_matches = (release['facility_id'] == snapshot['facility_id'] == payload['facility_id']
                                and release['from_day'] == snapshot['from_day'] == payload['from_day']
                                and release['to_day'] == snapshot['to_day'] == payload['to_day']
                                and release['source_revision'] == snapshot['source_revision']
                                == payload['source_revision'] and snapshot['state'] == 'published')
            return {'version': version, 'snapshot_id': snapshot['snapshot_id'],
                    'digest_matches': digest_matches, 'rows_match': rows_match,
                    'metadata_matches': metadata_matches, 'row_count': len(materialized),
                    'valid': digest_matches and rows_match and metadata_matches}
