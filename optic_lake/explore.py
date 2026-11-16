from collections import defaultdict
from decimal import Decimal

from .errors import Invalid, check
from .storage import Storage
from .values import decstr, iso_day, json_row, key, stage


class Explorer:
    def __init__(self, storage: Storage):
        self.storage = storage

    def events(self, facility_id, from_day, to_day, *, family_id=None, lot_id=None,
               process_stage=None, source_id=None, after_revision=0, limit=100):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid event window')
        check(type(after_revision) is int and after_revision >= 0, 'invalid revision cursor')
        check(type(limit) is int and 1 <= limit <= 500, 'limit must be 1-500')
        filters = ['facility_id=?', 'event_day BETWEEN ? AND ?', 'revision>?']
        params = [facility_id, from_day, to_day, after_revision]
        for column, value in (('family_id', family_id), ('lot_id', lot_id),
                              ('source_id', source_id)):
            if value is not None:
                filters.append(column + '=?')
                params.append(key(value, column))
        if process_stage is not None:
            filters.append('stage=?')
            params.append(stage(process_stage))
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            where = ' AND '.join(filters)
            rows = [dict(row) for row in db.execute(
                f'SELECT * FROM accepted_events WHERE {where} ORDER BY revision LIMIT ?',
                (*params, limit + 1))]
            more = len(rows) > limit
            visible = rows[:limit]
            return {'events': visible, 'next_revision': visible[-1]['revision'] if more else None,
                    'has_more': more, 'source_revision': self.storage.revision(db),
                    'filter': {'facility_id': facility_id, 'from_day': from_day, 'to_day': to_day,
                               'family_id': family_id, 'lot_id': lot_id, 'stage': process_stage,
                               'source_id': source_id}}

    def release_rows(self, version, *, family_id=None, lot_id=None,
                     process_stage=None, from_day=None, to_day=None, limit=500):
        check(type(version) is int and version > 0, 'invalid release version')
        check(type(limit) is int and 1 <= limit <= 1000, 'limit must be 1-1000')
        if family_id is not None:
            family_id = key(family_id, 'family_id')
        if lot_id is not None:
            lot_id = key(lot_id, 'lot_id')
        if process_stage is not None:
            process_stage = stage(process_stage)
        if from_day is not None:
            from_day = iso_day(from_day, 'from_day')
        if to_day is not None:
            to_day = iso_day(to_day, 'to_day')
        if from_day and to_day:
            check(from_day <= to_day, 'invalid release row window')
        with self.storage.read() as db:
            release = self.storage.get(db, 'SELECT * FROM releases WHERE version=?',
                                       (version,), 'release')
            results = []
            for record in db.execute('SELECT data_json FROM release_rows WHERE version=? ORDER BY row_key',
                                     (version,)):
                row = json_row(record['data_json'])
                if family_id and row['family_id'] != family_id:
                    continue
                if lot_id and row['lot_id'] != lot_id:
                    continue
                if process_stage and row['stage'] != process_stage:
                    continue
                if from_day and row['day'] < from_day:
                    continue
                if to_day and row['day'] > to_day:
                    continue
                results.append(row)
                if len(results) > limit:
                    break
            return {'version': version, 'snapshot_id': release['snapshot_id'],
                    'rows_digest': release['rows_digest'], 'rows': results[:limit],
                    'truncated': len(results) > limit}

    def summary(self, facility_id, from_day, to_day, group_by='stage', source_revision=None):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid summary window')
        check(group_by in {'stage', 'family_id', 'station_id', 'event_day', 'source_id'},
              'unsupported grouping')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            maximum = self.storage.revision(db)
            source_revision = maximum if source_revision is None else source_revision
            check(type(source_revision) is int and 0 <= source_revision <= maximum,
                  'invalid source revision')
            events = [dict(row) for row in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND event_day BETWEEN ? AND ? '
                'AND revision<=? ORDER BY revision',
                (facility_id, from_day, to_day, source_revision))]
        grouped = defaultdict(lambda: {'units': Decimal(0), 'passed_units': Decimal(0),
                                       'rejected_units': Decimal(0), 'event_count': 0,
                                       'lot_ids': set()})
        for event in events:
            bucket = grouped[event[group_by]]
            for field in ('units', 'passed_units', 'rejected_units'):
                bucket[field] += Decimal(event[field])
            bucket['event_count'] += 1
            bucket['lot_ids'].add(event['lot_id'])
        rows = []
        for value, totals in sorted(grouped.items()):
            rows.append({'group': value, 'units': decstr(totals['units']),
                         'passed_units': decstr(totals['passed_units']),
                         'rejected_units': decstr(totals['rejected_units']),
                         'event_count': totals['event_count'],
                         'lot_count': len(totals['lot_ids'])})
        return {'facility_id': facility_id, 'from_day': from_day, 'to_day': to_day,
                'group_by': group_by, 'source_revision': source_revision,
                'event_count': len(events), 'rows': rows}

    def coverage(self, facility_id, from_day, to_day):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid coverage window')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            sources = [dict(r) for r in db.execute(
                'SELECT * FROM sources WHERE facility_id=? ORDER BY stage,source_id',
                (facility_id,))]
            coverage = []
            for source in sources:
                contracts = [dict(r) for r in db.execute(
                    'SELECT contract_id,effective_day,retired_day FROM contracts WHERE source_id=? '
                    'ORDER BY effective_day', (source['source_id'],))]
                event_count = db.execute(
                    'SELECT count(*) FROM accepted_events WHERE source_id=? '
                    'AND event_day BETWEEN ? AND ?',
                    (source['source_id'], from_day, to_day)).fetchone()[0]
                batch_count = db.execute(
                    'SELECT count(*) FROM raw_batches WHERE source_id=? '
                    'AND substr(submitted_at,1,10) BETWEEN ? AND ?',
                    (source['source_id'], from_day, to_day)).fetchone()[0]
                intervals = []
                for contract in contracts:
                    start = max(from_day, contract['effective_day'])
                    end = min(to_day, contract['retired_day'] or to_day)
                    if start <= end:
                        intervals.append({'contract_id': contract['contract_id'],
                                          'effective_day': contract['effective_day'],
                                          'retired_day': contract['retired_day'],
                                          'overlap_from': start, 'overlap_to': end})
                coverage.append({'source_id': source['source_id'], 'stage': source['stage'],
                                 'enabled': bool(source['enabled']), 'contract_windows': intervals,
                                 'event_count': event_count, 'batch_count': batch_count})
            return {'facility_id': facility_id, 'from_day': from_day, 'to_day': to_day,
                    'sources': coverage}
