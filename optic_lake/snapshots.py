import json

from .errors import Conflict, Invalid, Stale, check
from .lineage import Lineage
from .quality import Quality
from .storage import Storage
from .values import canonical, fingerprint, iso_day, json_row, key


class Snapshots:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.lineage = Lineage(storage)

    def prepare(self, snapshot_id, facility_id, from_day, to_day, expected_head=None):
        snapshot_id, facility_id = key(snapshot_id, 'snapshot_id'), key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid snapshot window')
        with self.storage.write() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            if db.execute('SELECT 1 FROM snapshots WHERE snapshot_id=?', (snapshot_id,)).fetchone():
                raise Conflict('snapshot id exists')
            head = self.storage.head(db)
            if expected_head is not None and (type(expected_head) is not int or expected_head != head):
                raise Stale('published head changed')
            source_revision = self.storage.revision(db)
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND event_day BETWEEN ? AND ? '
                'AND revision<=? ORDER BY family_id,lot_id,stage,source_id,external_id',
                (facility_id, from_day, to_day, source_revision))]
            links = [dict(r) for r in db.execute(
                'SELECT * FROM material_links WHERE facility_id=? AND created_revision<=? '
                'ORDER BY parent_lot,child_lot,link_id', (facility_id, source_revision))]
            material = self.lineage.reconcile_rows(events, links)
            process = Quality.analyze(facility_id, from_day, to_day, source_revision,
                                      events, links, [], [])
            blocking = {'stage_overflow', 'lineage_time_order', 'lineage_overflow',
                        'child_supply_overflow'}
            process_issues = [item for item in process['issues'] if item['code'] in blocking]
            if not material['rows']:
                raise Invalid('snapshot window contains no accepted events')
            if material['issues'] or process_issues:
                raise Invalid('cannot prepare unbalanced snapshot',
                              issues=material['issues'] + process_issues)
            payload = {'rows': material['rows'], 'lineage_inputs': material['lineage_inputs'],
                       'facility_id': facility_id, 'from_day': from_day, 'to_day': to_day,
                       'source_revision': source_revision}
            digest = fingerprint(payload)
            db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                       (snapshot_id, facility_id, from_day, to_day, source_revision,
                        head, canonical(payload), digest, 'prepared'))
        return {'snapshot_id': snapshot_id, 'source_revision': source_revision,
                'expected_head': head, 'digest': digest, 'row_count': len(material['rows']),
                'state': 'prepared'}

    def publish(self, snapshot_id):
        snapshot_id = key(snapshot_id, 'snapshot_id')
        with self.storage.write() as db:
            snapshot = self.storage.get(db, 'SELECT * FROM snapshots WHERE snapshot_id=?',
                                        (snapshot_id,), 'snapshot')
            if snapshot['state'] == 'published':
                release = self.storage.get(db, 'SELECT * FROM releases WHERE snapshot_id=?',
                                           (snapshot_id,), 'release')
                return self._release(release)
            if snapshot['expected_head'] != self.storage.head(db):
                raise Stale('snapshot prepared against another published head')
            if snapshot['source_revision'] != self.storage.revision(db):
                raise Stale('source revision changed since snapshot preparation')
            payload = json_row(snapshot['rows_json'])
            if fingerprint(payload) != snapshot['rows_digest']:
                raise Invalid('snapshot digest mismatch')
            version = self.storage.head(db) + 1
            db.execute('INSERT INTO releases(version,snapshot_id,facility_id,from_day,to_day,'
                       'source_revision,rows_digest) VALUES (?,?,?,?,?,?,?)',
                       (version, snapshot_id, snapshot['facility_id'], snapshot['from_day'],
                        snapshot['to_day'], snapshot['source_revision'], snapshot['rows_digest']))
            for row in payload['rows']:
                db.execute('INSERT INTO release_rows VALUES (?,?,?)',
                           (version, row['row_key'], canonical(row)))
            db.execute('UPDATE snapshots SET state=? WHERE snapshot_id=?', ('published', snapshot_id))
            db.execute('UPDATE clock SET published_head=? WHERE singleton=1', (version,))
            release = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                       (version,), 'release')
            return self._release(release)

    def snapshot(self, snapshot_id):
        snapshot_id = key(snapshot_id, 'snapshot_id')
        with self.storage.read() as db:
            snapshot = self.storage.get(db, 'SELECT * FROM snapshots WHERE snapshot_id=?',
                                        (snapshot_id,), 'snapshot')
            result = dict(snapshot)
            result['payload'] = json_row(result.pop('rows_json'))
            return result

    def release(self, version):
        check(type(version) is int and version > 0, 'version must be positive integer')
        with self.storage.read() as db:
            row = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                   (version,), 'release')
            result = self._release(row)
            result['rows'] = [json_row(r['data_json']) for r in db.execute(
                'SELECT data_json FROM release_rows WHERE version=? ORDER BY row_key', (version,))]
            return result

    def latest(self):
        with self.storage.read() as db:
            head = self.storage.head(db)
        return None if head == 0 else self.release(head)

    @staticmethod
    def _release(row):
        return dict(row)
