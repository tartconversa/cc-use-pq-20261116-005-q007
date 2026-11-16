from collections import defaultdict
from decimal import Decimal

from .errors import check
from .storage import Storage
from .values import STAGES, decstr, iso_day, key


class Overview:
    def __init__(self, storage: Storage):
        self.storage = storage

    def facility(self, facility_id, from_day, to_day):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid facility window')
        with self.storage.read() as db:
            facility = dict(self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                                             (facility_id,), 'facility'))
            families = [dict(r) for r in db.execute(
                'SELECT * FROM product_families WHERE facility_id=? ORDER BY family_id',
                (facility_id,))]
            stations = [dict(r) for r in db.execute(
                'SELECT * FROM stations WHERE facility_id=? ORDER BY stage,station_id',
                (facility_id,))]
            sources = [dict(r) for r in db.execute(
                'SELECT * FROM sources WHERE facility_id=? ORDER BY stage,source_id',
                (facility_id,))]
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND event_day BETWEEN ? AND ? '
                'ORDER BY event_day,source_id,external_id',
                (facility_id, from_day, to_day))]
            links = [dict(r) for r in db.execute(
                'SELECT * FROM material_links WHERE facility_id=? ORDER BY link_id',
                (facility_id,))]
            releases = [dict(r) for r in db.execute(
                'SELECT * FROM releases WHERE facility_id=? ORDER BY version', (facility_id,))]
            revision, head = self.storage.revision(db), self.storage.head(db)
        stage_totals = defaultdict(lambda: {'units': Decimal(0), 'passed': Decimal(0),
                                            'rejected': Decimal(0), 'lots': set()})
        for event in events:
            bucket = stage_totals[event['stage']]
            bucket['units'] += Decimal(event['units'])
            bucket['passed'] += Decimal(event['passed_units'])
            bucket['rejected'] += Decimal(event['rejected_units'])
            bucket['lots'].add(event['lot_id'])
        stage_rows = []
        for name in STAGES:
            totals = stage_totals[name]
            stage_rows.append({'stage': name, 'units': decstr(totals['units']),
                               'passed_units': decstr(totals['passed']),
                               'rejected_units': decstr(totals['rejected']),
                               'lot_count': len(totals['lots']),
                               'station_count': sum(s['stage'] == name for s in stations),
                               'source_count': sum(s['stage'] == name for s in sources)})
        return {'facility': facility, 'from_day': from_day, 'to_day': to_day,
                'source_revision': revision, 'published_head': head,
                'families': families, 'stations': stations, 'sources': sources,
                'event_count': len(events), 'material_link_count': len(links),
                'release_versions': [r['version'] for r in releases],
                'stages': stage_rows}

    def family(self, family_id, from_day, to_day):
        family_id = key(family_id, 'family_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid family window')
        with self.storage.read() as db:
            family = dict(self.storage.get(db, 'SELECT * FROM product_families WHERE family_id=?',
                                           (family_id,), 'family'))
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE family_id=? AND event_day BETWEEN ? AND ? '
                'ORDER BY lot_id,stage,occurred_at', (family_id, from_day, to_day))]
            lots = [dict(r) for r in db.execute(
                'SELECT * FROM production_lots WHERE family_id=? ORDER BY created_day,lot_id',
                (family_id,))]
        by_lot = defaultdict(list)
        for event in events:
            by_lot[event['lot_id']].append(event)
        summaries = []
        for lot in lots:
            records = by_lot[lot['lot_id']]
            if not records:
                continue
            stages = sorted(set(record['stage'] for record in records),
                            key=lambda stage: STAGES.index(stage))
            summaries.append({'lot_id': lot['lot_id'], 'created_day': lot['created_day'],
                              'event_count': len(records), 'stages': stages,
                              'last_stage': stages[-1],
                              'last_event_at': max(record['occurred_at'] for record in records),
                              'passed_units': decstr(sum((Decimal(record['passed_units'])
                                                        for record in records if record['stage'] == stages[-1]),
                                                       Decimal(0)))})
        return {'family': family, 'from_day': from_day, 'to_day': to_day,
                'event_count': len(events), 'lots': summaries}
