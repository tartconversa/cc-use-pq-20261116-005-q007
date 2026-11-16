from collections import defaultdict
from decimal import Decimal

from .storage import Storage
from .values import fingerprint, json_row


class Integrity:
    def __init__(self, storage: Storage):
        self.storage = storage

    def inspect(self):
        with self.storage.read() as db:
            counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('facilities', 'product_families', 'stations', 'sources',
                                    'contracts', 'raw_batches', 'raw_rows', 'accepted_events',
                                    'production_lots', 'material_links', 'snapshots',
                                    'releases', 'release_rows')}
            checks = []
            checks.extend(self._foreign_keys(db))
            checks.extend(self._raw_batches(db))
            checks.extend(self._accepted_events(db))
            checks.extend(self._material_links(db))
            checks.extend(self._snapshots(db))
            checks.extend(self._releases(db))
            revision = self.storage.revision(db)
            head = self.storage.head(db)
        return {'valid': not checks, 'source_revision': revision, 'published_head': head,
                'counts': counts, 'issues': checks}

    @staticmethod
    def _foreign_keys(db):
        return [{'code': 'foreign_key_violation', 'table': row[0],
                 'rowid': row[1], 'parent': row[2]}
                for row in db.execute('PRAGMA foreign_key_check')]

    @staticmethod
    def _raw_batches(db):
        issues = []
        for batch in db.execute('SELECT * FROM raw_batches ORDER BY batch_id'):
            rows = [dict(r) for r in db.execute('SELECT * FROM raw_rows WHERE batch_id=? '
                                              'ORDER BY ordinal', (batch['batch_id'],))]
            result = json_row(batch['result_json'])
            expected = {'accepted': 0, 'duplicate': 0, 'quarantined': 0}
            for index, row in enumerate(rows):
                if row['ordinal'] != index:
                    issues.append({'code': 'ordinal_gap', 'batch_id': batch['batch_id'],
                                   'ordinal': row['ordinal']})
                expected[row['outcome']] += 1
                if fingerprint(json_row(row['raw_json'])) != row['raw_digest']:
                    issues.append({'code': 'raw_digest_mismatch', 'batch_id': batch['batch_id'],
                                   'ordinal': row['ordinal']})
                if row['outcome'] == 'accepted' and row['event_revision'] is None:
                    issues.append({'code': 'accepted_without_revision', 'batch_id': batch['batch_id'],
                                   'ordinal': row['ordinal']})
                if row['outcome'] != 'accepted' and row['event_revision'] is not None:
                    issues.append({'code': 'unaccepted_with_revision', 'batch_id': batch['batch_id'],
                                   'ordinal': row['ordinal']})
            if len(rows) != len(result.get('rows', [])) or any(
                    result.get(name) != count for name, count in expected.items()):
                issues.append({'code': 'batch_count_mismatch', 'batch_id': batch['batch_id']})
            if batch['source_revision'] > db.execute('SELECT revision FROM clock WHERE singleton=1').fetchone()[0]:
                issues.append({'code': 'future_batch_revision', 'batch_id': batch['batch_id']})
        return issues

    @staticmethod
    def _accepted_events(db):
        issues = []
        for event in db.execute('SELECT * FROM accepted_events ORDER BY source_id,external_id'):
            identity = {'source_id': event['source_id'], 'external_id': event['external_id']}
            raw = db.execute('SELECT * FROM raw_rows WHERE batch_id=? AND ordinal=?',
                             (event['batch_id'], event['ordinal'])).fetchone()
            if raw is None or raw['outcome'] != 'accepted' or raw['event_revision'] != event['revision']:
                issues.append({'code': 'event_raw_mismatch', **identity})
            if Decimal(event['units']) != Decimal(event['passed_units']) + Decimal(event['rejected_units']):
                issues.append({'code': 'event_quantity_mismatch', **identity})
            lot = db.execute('SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                             (event['facility_id'], event['lot_id'])).fetchone()
            if lot is None or lot['family_id'] != event['family_id']:
                issues.append({'code': 'event_lot_mismatch', **identity})
            source = db.execute('SELECT * FROM sources WHERE source_id=?', (event['source_id'],)).fetchone()
            station = db.execute('SELECT * FROM stations WHERE station_id=?', (event['station_id'],)).fetchone()
            if source is None or station is None or source['stage'] != event['stage'] or station['stage'] != event['stage']:
                issues.append({'code': 'event_stage_mismatch', **identity})
        return issues

    @staticmethod
    def _material_links(db):
        issues = []
        consumed = defaultdict(Decimal)
        for link in db.execute('SELECT * FROM material_links ORDER BY link_id'):
            identity = {'link_id': link['link_id']}
            if link['parent_lot'] == link['child_lot']:
                issues.append({'code': 'self_link', **identity})
            consumed[(link['facility_id'], link['parent_lot'])] += Decimal(link['used_units'])
            parent = db.execute('SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                                (link['facility_id'], link['parent_lot'])).fetchone()
            child = db.execute('SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                               (link['facility_id'], link['child_lot'])).fetchone()
            if parent is None or child is None or parent['family_id'] != child['family_id']:
                issues.append({'code': 'link_family_mismatch', **identity})
        for (facility_id, lot_id), quantity in sorted(consumed.items()):
            stage_passed = defaultdict(Decimal)
            for row in db.execute('SELECT stage,passed_units FROM accepted_events '
                                  'WHERE facility_id=? AND lot_id=?', (facility_id, lot_id)):
                stage_passed[row['stage']] += Decimal(row['passed_units'])
            if quantity > max(stage_passed.values(), default=Decimal(0)):
                issues.append({'code': 'link_overconsumed', 'facility_id': facility_id,
                               'lot_id': lot_id})
        return issues

    @staticmethod
    def _snapshots(db):
        issues = []
        for snap in db.execute('SELECT * FROM snapshots ORDER BY snapshot_id'):
            payload = json_row(snap['rows_json'])
            if fingerprint(payload) != snap['rows_digest']:
                issues.append({'code': 'snapshot_digest_mismatch', 'snapshot_id': snap['snapshot_id']})
            if snap['source_revision'] != payload.get('source_revision'):
                issues.append({'code': 'snapshot_revision_mismatch', 'snapshot_id': snap['snapshot_id']})
            if (snap['facility_id'], snap['from_day'], snap['to_day']) != (
                    payload.get('facility_id'), payload.get('from_day'), payload.get('to_day')):
                issues.append({'code': 'snapshot_metadata_mismatch', 'snapshot_id': snap['snapshot_id']})
            release = db.execute('SELECT * FROM releases WHERE snapshot_id=?',
                                 (snap['snapshot_id'],)).fetchone()
            if (snap['state'] == 'published') != (release is not None):
                issues.append({'code': 'snapshot_release_state_mismatch', 'snapshot_id': snap['snapshot_id']})
            if not payload.get('rows'):
                issues.append({'code': 'snapshot_without_rows', 'snapshot_id': snap['snapshot_id']})
        return issues

    @staticmethod
    def _releases(db):
        issues = []
        releases = [dict(r) for r in db.execute('SELECT * FROM releases ORDER BY version')]
        head = db.execute('SELECT published_head FROM clock WHERE singleton=1').fetchone()[0]
        if head != len(releases):
            issues.append({'code': 'release_head_mismatch', 'head': head,
                           'release_count': len(releases)})
        for expected, release in enumerate(releases, 1):
            if release['version'] != expected:
                issues.append({'code': 'release_version_gap', 'version': release['version']})
            snapshot = db.execute('SELECT * FROM snapshots WHERE snapshot_id=?',
                                  (release['snapshot_id'],)).fetchone()
            if snapshot is None:
                issues.append({'code': 'release_without_snapshot', 'version': release['version']})
                continue
            expected_rows = {row['row_key']: row for row in json_row(snapshot['rows_json'])['rows']}
            materialized = {r['row_key']: json_row(r['data_json']) for r in db.execute(
                'SELECT * FROM release_rows WHERE version=?', (release['version'],))}
            if materialized != expected_rows:
                issues.append({'code': 'release_rows_mismatch', 'version': release['version']})
            if release['rows_digest'] != snapshot['rows_digest']:
                issues.append({'code': 'release_digest_mismatch', 'version': release['version']})
        return issues
