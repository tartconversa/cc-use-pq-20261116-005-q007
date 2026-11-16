from collections import defaultdict
from decimal import Decimal

from .errors import check
from .storage import Storage
from .values import STAGES, decstr, iso_day, key


class Quality:
    def __init__(self, storage: Storage):
        self.storage = storage

    def report(self, facility_id, from_day, to_day, source_revision=None):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid quality window')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            maximum = self.storage.revision(db)
            source_revision = maximum if source_revision is None else source_revision
            check(type(source_revision) is int and 0 <= source_revision <= maximum,
                  'invalid source revision')
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND event_day BETWEEN ? AND ? '
                'AND revision<=? ORDER BY occurred_at,source_id,external_id',
                (facility_id, from_day, to_day, source_revision))]
            links = [dict(r) for r in db.execute(
                'SELECT * FROM material_links WHERE facility_id=? AND created_revision<=? '
                'ORDER BY link_id', (facility_id, source_revision))]
            raw = [dict(r) for r in db.execute(
                'SELECT b.source_id,b.batch_id,r.ordinal,r.outcome,r.reason '
                'FROM raw_rows r JOIN raw_batches b ON b.batch_id=r.batch_id '
                'WHERE b.source_id IN (SELECT source_id FROM sources WHERE facility_id=?) '
                'AND substr(b.submitted_at,1,10) BETWEEN ? AND ? AND b.source_revision<=? '
                'ORDER BY b.source_id,b.batch_id,r.ordinal',
                (facility_id, from_day, to_day, source_revision))]
            sources = [dict(r) for r in db.execute(
                'SELECT * FROM sources WHERE facility_id=? ORDER BY source_id', (facility_id,))]
        return self.analyze(facility_id, from_day, to_day, source_revision,
                            events, links, raw, sources)

    @staticmethod
    def analyze(facility_id, from_day, to_day, source_revision, events, links, raw, sources):
        stage_index = {stage: index for index, stage in enumerate(STAGES)}
        by_lot = defaultdict(list)
        by_source = defaultdict(list)
        for event in events:
            by_lot[event['lot_id']].append(event)
            by_source[event['source_id']].append(event)
        raw_by_source = defaultdict(list)
        for record in raw:
            raw_by_source[record['source_id']].append(record)
        issues = []
        lot_reports = []
        for lot_id, records in sorted(by_lot.items()):
            records.sort(key=lambda r: (r['occurred_at'], r['source_id'], r['external_id']))
            first = records[0]
            stage_totals = defaultdict(lambda: {'units': Decimal(0), 'passed': Decimal(0),
                                                'rejected': Decimal(0), 'events': 0})
            for event in records:
                bucket = stage_totals[event['stage']]
                bucket['units'] += Decimal(event['units'])
                bucket['passed'] += Decimal(event['passed_units'])
                bucket['rejected'] += Decimal(event['rejected_units'])
                bucket['events'] += 1
            stage_names = sorted(stage_totals, key=lambda name: stage_index[name])
            for before, after in zip(stage_names, stage_names[1:]):
                if stage_index[after] > stage_index[before] + 1:
                    issues.append({'code': 'stage_gap', 'lot_id': lot_id,
                                   'earlier_stage': before, 'later_stage': after})
                if stage_totals[after]['units'] > stage_totals[before]['passed']:
                    issues.append({'code': 'stage_overflow', 'lot_id': lot_id,
                                   'earlier_stage': before, 'later_stage': after,
                                   'available': decstr(stage_totals[before]['passed']),
                                   'observed': decstr(stage_totals[after]['units'])})
            last_stage = stage_names[-1]
            lot_reports.append({'lot_id': lot_id, 'family_id': first['family_id'],
                                'first_event': records[0]['occurred_at'],
                                'last_event': records[-1]['occurred_at'],
                                'current_stage': last_stage,
                                'yield_units': decstr(stage_totals[last_stage]['passed']),
                                'stages': [{'stage': stage, 'units': decstr(stage_totals[stage]['units']),
                                            'passed_units': decstr(stage_totals[stage]['passed']),
                                            'rejected_units': decstr(stage_totals[stage]['rejected']),
                                            'event_count': stage_totals[stage]['events']}
                                           for stage in stage_names]})
        link_totals = defaultdict(Decimal)
        child_input = defaultdict(Decimal)
        for link in links:
            link_totals[link['parent_lot']] += Decimal(link['used_units'])
            child_input[link['child_lot']] += Decimal(link['used_units'])
            if link['parent_lot'] in by_lot and link['child_lot'] in by_lot:
                last_parent = max(by_lot[link['parent_lot']], key=lambda r: r['occurred_at'])
                first_child = min(by_lot[link['child_lot']], key=lambda r: r['occurred_at'])
                if last_parent['occurred_at'] > first_child['occurred_at']:
                    issues.append({'code': 'lineage_time_order', 'link_id': link['link_id'],
                                   'parent_lot': link['parent_lot'], 'child_lot': link['child_lot']})
        for lot_id, consumed in sorted(link_totals.items()):
            if lot_id not in by_lot:
                continue
            parent_passed = max((Decimal(r['passed_units']) for r in by_lot[lot_id]),
                                default=Decimal(0))
            if consumed > parent_passed:
                issues.append({'code': 'lineage_overflow', 'lot_id': lot_id,
                               'consumed': decstr(consumed), 'passed': decstr(parent_passed)})
        for lot_id, supplied in sorted(child_input.items()):
            if lot_id not in by_lot:
                continue
            first_stage = min(stage_index[r['stage']] for r in by_lot[lot_id])
            first_units = sum((Decimal(r['units']) for r in by_lot[lot_id]
                               if stage_index[r['stage']] == first_stage), Decimal(0))
            if supplied > first_units:
                issues.append({'code': 'child_supply_overflow', 'lot_id': lot_id,
                               'supplied': decstr(supplied), 'first_stage_units': decstr(first_units)})
        source_reports = []
        for source in sources:
            sid = source['source_id']
            records = raw_by_source[sid]
            accepted = sum(item['outcome'] == 'accepted' for item in records)
            duplicate = sum(item['outcome'] == 'duplicate' for item in records)
            quarantined = sum(item['outcome'] == 'quarantined' for item in records)
            source_reports.append({'source_id': sid, 'stage': source['stage'],
                                   'enabled': bool(source['enabled']),
                                   'raw_rows': len(records), 'accepted_rows': accepted,
                                   'duplicate_rows': duplicate, 'quarantined_rows': quarantined,
                                   'event_count_in_window': len(by_source[sid])})
        return {'facility_id': facility_id, 'from_day': from_day, 'to_day': to_day,
                'source_revision': source_revision, 'lot_count': len(lot_reports),
                'event_count': len(events), 'issues': issues, 'lots': lot_reports,
                'sources': source_reports}

    def source_detail(self, source_id):
        source_id = key(source_id, 'source_id')
        with self.storage.read() as db:
            source = dict(self.storage.get(db, 'SELECT * FROM sources WHERE source_id=?',
                                           (source_id,), 'source'))
            counts = {r['outcome']: r['count'] for r in db.execute(
                'SELECT r.outcome,count(*) AS count FROM raw_rows r JOIN raw_batches b '
                'ON r.batch_id=b.batch_id WHERE b.source_id=? GROUP BY r.outcome', (source_id,))}
            source['raw_outcomes'] = {name: counts.get(name, 0)
                                      for name in ('accepted', 'duplicate', 'quarantined')}
            source['accepted_events'] = db.execute(
                'SELECT count(*) FROM accepted_events WHERE source_id=?', (source_id,)).fetchone()[0]
            source['contract_count'] = db.execute(
                'SELECT count(*) FROM contracts WHERE source_id=?', (source_id,)).fetchone()[0]
            source['last_event_at'] = db.execute(
                'SELECT max(occurred_at) FROM accepted_events WHERE source_id=?', (source_id,)).fetchone()[0]
            source['last_batch_at'] = db.execute(
                'SELECT max(submitted_at) FROM raw_batches WHERE source_id=?', (source_id,)).fetchone()[0]
            return source
