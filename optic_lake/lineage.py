from collections import defaultdict
from decimal import Decimal

from .errors import Conflict, Invalid, check
from .storage import Storage
from .values import decstr, iso_day, key, number


class Lineage:
    def __init__(self, storage: Storage):
        self.storage = storage

    def link(self, link_id, facility_id, parent_lot, child_lot, used_units):
        link_id, facility_id = key(link_id, 'link_id'), key(facility_id, 'facility_id')
        parent_lot, child_lot = key(parent_lot, 'parent_lot'), key(child_lot, 'child_lot')
        check(parent_lot != child_lot, 'lot cannot feed itself')
        used_units = number(used_units, 'used_units', positive=True)
        check(used_units == used_units.to_integral_value(), 'used units must be whole')
        with self.storage.write() as db:
            if db.execute('SELECT 1 FROM material_links WHERE link_id=?', (link_id,)).fetchone():
                raise Conflict('material link exists')
            parent = self._lot(db, facility_id, parent_lot)
            child = self._lot(db, facility_id, child_lot)
            check(parent['family_id'] == child['family_id'], 'linked lots need same product family')
            check(parent['created_day'] <= child['created_day'], 'parent lot must exist before child')
            if child_lot in self._ancestors(db, facility_id, parent_lot):
                raise Conflict('material link would form a cycle')
            # A child may combine several upstream lots, but no parent can be consumed
            # beyond its observed passed output.
            output = self._passed(db, facility_id, parent_lot)
            consumed = self._consumed(db, facility_id, parent_lot)
            check(consumed + used_units <= output, 'parent lot has insufficient passed units')
            revision = self.storage.advance(db, 'material-link', link_id)
            db.execute('INSERT INTO material_links VALUES (?,?,?,?,?,?)',
                       (link_id, facility_id, parent_lot, child_lot, decstr(used_units), revision))
        return {'link_id': link_id, 'revision': revision}

    def lot(self, facility_id, lot_id):
        facility_id, lot_id = key(facility_id, 'facility_id'), key(lot_id, 'lot_id')
        with self.storage.read() as db:
            lot = dict(self._lot(db, facility_id, lot_id))
            lot['ancestors'] = sorted(self._ancestors(db, facility_id, lot_id))
            lot['children'] = [dict(r) for r in db.execute(
                'SELECT link_id,child_lot,used_units FROM material_links '
                'WHERE facility_id=? AND parent_lot=? ORDER BY link_id', (facility_id, lot_id))]
            lot['inputs'] = [dict(r) for r in db.execute(
                'SELECT link_id,parent_lot,used_units FROM material_links '
                'WHERE facility_id=? AND child_lot=? ORDER BY link_id', (facility_id, lot_id))]
            lot['passed_units'] = decstr(self._passed(db, facility_id, lot_id))
            lot['consumed_units'] = decstr(self._consumed(db, facility_id, lot_id))
            return lot

    def reconcile(self, facility_id, from_day, to_day, source_revision=None):
        facility_id = key(facility_id, 'facility_id')
        from_day, to_day = iso_day(from_day, 'from_day'), iso_day(to_day, 'to_day')
        check(from_day <= to_day, 'invalid date window')
        with self.storage.read() as db:
            self.storage.get(db, 'SELECT * FROM facilities WHERE facility_id=?',
                             (facility_id,), 'facility')
            if source_revision is None:
                source_revision = self.storage.revision(db)
            check(type(source_revision) is int and 0 <= source_revision <= self.storage.revision(db),
                  'invalid source revision')
            events = [dict(r) for r in db.execute(
                'SELECT * FROM accepted_events WHERE facility_id=? AND event_day BETWEEN ? AND ? '
                'AND revision<=? ORDER BY family_id,lot_id,stage,source_id,external_id',
                (facility_id, from_day, to_day, source_revision))]
            links = [dict(r) for r in db.execute(
                'SELECT * FROM material_links WHERE facility_id=? AND created_revision<=? '
                'ORDER BY parent_lot,child_lot,link_id', (facility_id, source_revision))]
        return self.reconcile_rows(events, links)

    @staticmethod
    def reconcile_rows(events, links):
        grouped = defaultdict(lambda: {'units': Decimal(0), 'passed': Decimal(0),
                                       'rejected': Decimal(0), 'event_count': 0, 'sources': set(),
                                       'stations': set(), 'measurement_total': Decimal(0),
                                       'measurement_count': 0})
        by_lot = defaultdict(lambda: defaultdict(Decimal))
        issues = []
        for event in events:
            group_key = (event['facility_id'], event['family_id'], event['lot_id'], event['stage'], event['event_day'])
            bucket = grouped[group_key]
            units, passed, rejected = (Decimal(event[field]) for field in
                                       ('units', 'passed_units', 'rejected_units'))
            bucket['units'] += units
            bucket['passed'] += passed
            bucket['rejected'] += rejected
            bucket['event_count'] += 1
            bucket['sources'].add(event['source_id'])
            bucket['stations'].add(event['station_id'])
            by_lot[(event['facility_id'], event['lot_id'])][event['stage']] += passed
            if event['measured_value'] is not None:
                bucket['measurement_total'] += Decimal(event['measured_value'])
                bucket['measurement_count'] += 1
            if units != passed + rejected:
                issues.append({'code': 'row_balance', 'lot_id': event['lot_id'],
                               'event_id': event['external_id']})
        rows = []
        for identity, totals in sorted(grouped.items()):
            facility_id, family_id, lot_id, stage, day = identity
            mean = (totals['measurement_total'] / totals['measurement_count']
                    if totals['measurement_count'] else None)
            rows.append({'row_key': '|'.join(identity), 'facility_id': facility_id,
                         'family_id': family_id, 'lot_id': lot_id, 'stage': stage, 'day': day,
                         'units': decstr(totals['units']), 'passed_units': decstr(totals['passed']),
                         'rejected_units': decstr(totals['rejected']),
                         'event_count': totals['event_count'], 'source_ids': sorted(totals['sources']),
                         'station_ids': sorted(totals['stations']),
                         'measurement_mean_um': decstr(mean) if mean is not None else None})
        used = defaultdict(Decimal)
        parents = defaultdict(list)
        for link in links:
            parent = (link['facility_id'], link['parent_lot'])
            child = (link['facility_id'], link['child_lot'])
            qty = Decimal(link['used_units'])
            used[parent] += qty
            parents[child].append({'parent_lot': link['parent_lot'], 'used_units': decstr(qty)})
        for lot, used_units in sorted(used.items()):
            output = max(by_lot[lot].values(), default=Decimal(0))
            if used_units > output:
                issues.append({'code': 'over_consumed', 'lot_id': lot[1],
                               'used_units': decstr(used_units), 'available_units': decstr(output)})
        return {'rows': rows, 'issues': issues, 'lineage_inputs': [
            {'facility_id': lot[0], 'lot_id': lot[1], 'parents': sorted(items, key=lambda i: i['parent_lot'])}
            for lot, items in sorted(parents.items())]}

    def _lot(self, db, facility_id, lot_id):
        return self.storage.get(db, 'SELECT * FROM production_lots WHERE facility_id=? AND lot_id=?',
                                (facility_id, lot_id), 'lot')

    @staticmethod
    def _passed(db, facility_id, lot_id):
        rows = db.execute('SELECT stage,passed_units FROM accepted_events WHERE facility_id=? AND lot_id=?',
                          (facility_id, lot_id))
        stages = defaultdict(Decimal)
        for row in rows:
            stages[row['stage']] += Decimal(row['passed_units'])
        return max(stages.values(), default=Decimal(0))

    @staticmethod
    def _consumed(db, facility_id, lot_id):
        return sum((Decimal(r[0]) for r in db.execute(
            'SELECT used_units FROM material_links WHERE facility_id=? AND parent_lot=?',
            (facility_id, lot_id))), Decimal(0))

    @staticmethod
    def _ancestors(db, facility_id, lot_id):
        found = set()
        stack = [lot_id]
        while stack:
            current = stack.pop()
            for row in db.execute('SELECT parent_lot FROM material_links WHERE facility_id=? AND child_lot=?',
                                  (facility_id, current)):
                parent = row[0]
                if parent not in found:
                    found.add(parent)
                    stack.append(parent)
        return found
