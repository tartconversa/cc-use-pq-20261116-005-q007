from optic_lake.errors import Invalid, Missing
from tests.support import LakeCase


class QualityTests(LakeCase):
    def test_report_shows_accepted_rows_and_quarantine(self):
        self.linked()
        report = self.quality.report('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(report['event_count'], 2)
        self.assertEqual(report['lot_count'], 2)
        mold = next(row for row in report['sources'] if row['source_id'] == 'SRC_M')
        coat = next(row for row in report['sources'] if row['source_id'] == 'SRC_C')
        self.assertEqual(mold['quarantined_rows'], 1)
        self.assertEqual(coat['accepted_rows'], 1)

    def test_report_flags_stage_gap_in_same_lot(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        coat = self.new_event('COAT_SAME', station='S_C', units=80, passed=78)
        self.submit_one('COAT_SAME', coat, source='SRC_C')
        report = self.quality.report('F1', '2026-02-01', '2026-02-02')
        self.assertTrue(any(i['code'] == 'stage_gap' for i in report['issues']))
        self.assertEqual(report['lots'][0]['current_stage'], 'coat')

    def test_source_detail_counts_all_row_outcomes(self):
        self.load()
        self.submit_one('DUP', self.fixture['batches'][0]['rows'][0])
        detail = self.quality.source_detail('SRC_M')
        self.assertEqual(detail['raw_outcomes'],
                         {'accepted': 1, 'duplicate': 1, 'quarantined': 1})
        self.assertEqual(detail['accepted_events'], 1)
        self.assertEqual(detail['contract_count'], 1)

    def test_revision_cutoff_excludes_later_event(self):
        self.load()
        revision = self.storage.state()['source_revision']
        self.submit_one('LATE', self.new_event('LATE'))
        old = self.quality.report('F1', '2026-02-01', '2026-02-02', revision)
        new = self.quality.report('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(new['event_count'], old['event_count'] + 1)


class AuditTests(LakeCase):
    def test_timeline_records_catalog_batch_event_and_link(self):
        self.linked()
        result = self.audit.timeline()
        kinds = [record['kind'] for record in result['records']]
        self.assertIn('contract', kinds)
        self.assertIn('raw-batch', kinds)
        self.assertIn('event', kinds)
        self.assertIn('material-link', kinds)

    def test_event_history_connects_lot_and_raw_identity(self):
        self.linked()
        result = self.audit.event_history('F1', 'L_M')
        self.assertEqual(result['events'][0]['batch_id'], 'B_M')
        self.assertEqual(result['links'][0]['child_lot'], 'L_C')

    def test_batch_history_checks_outcome_count(self):
        self.load()
        result = self.audit.batch_history('SRC_M', '2026-02-01', '2026-02-02')
        self.assertEqual(result['batch_count'], 1)
        self.assertEqual(result['batches'][0]['counts']['quarantined'], 1)

    def test_release_verify_matches_frozen_materialization(self):
        self.published()
        result = self.audit.verify_release(1)
        self.assertTrue(result['valid'])
        self.assertEqual(result['row_count'], 2)

    def test_release_compare_detects_new_row(self):
        self.published()
        self.submit_one('NEXT', self.new_event('NEXT', lot='L_NEW'))
        self.snapshots.prepare('SECOND', 'F1', '2026-02-01', '2026-02-02')
        self.snapshots.publish('SECOND')
        difference = self.audit.compare_releases(1, 2)
        self.assertEqual(len(difference['added']), 1)
        self.assertEqual(difference['added'][0]['lot_id'], 'L_NEW')
        self.assertEqual(difference['unchanged_count'], 2)

    def test_release_compare_detects_changed_quantity(self):
        self.published()
        self.submit_one('MORE', self.new_event('MORE'))
        self.snapshots.prepare('SECOND', 'F1', '2026-02-01', '2026-02-02')
        self.snapshots.publish('SECOND')
        difference = self.audit.compare_releases(1, 2)
        self.assertEqual(len(difference['changed']), 1)
        self.assertEqual(difference['changed'][0]['quantity_delta']['units'], '10')

    def test_timeline_rejects_invalid_range(self):
        self.register()
        with self.assertRaises(Invalid):
            self.audit.timeline(7, 6)


class ExplorerTests(LakeCase):
    def test_event_cursor_returns_next_revision(self):
        self.load()
        self.submit_one('NEXT', self.new_event('NEXT'))
        first = self.explore.events('F1', '2026-02-01', '2026-02-02', limit=1)
        self.assertTrue(first['has_more'])
        second = self.explore.events('F1', '2026-02-01', '2026-02-02',
                                     after_revision=first['next_revision'], limit=1)
        self.assertEqual(second['events'][0]['external_id'], 'C1')

    def test_event_filter_restricts_stage(self):
        self.load()
        result = self.explore.events('F1', '2026-02-01', '2026-02-02',
                                     process_stage='coat')
        self.assertEqual([row['external_id'] for row in result['events']], ['C1'])

    def test_release_row_filter_selects_lot(self):
        self.published()
        result = self.explore.release_rows(1, lot_id='L_M')
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['lot_id'], 'L_M')

    def test_summary_groups_and_balances_quantities(self):
        self.load()
        result = self.explore.summary('F1', '2026-02-01', '2026-02-02', 'stage')
        self.assertEqual(result['event_count'], 2)
        self.assertEqual({row['group'] for row in result['rows']}, {'mold', 'coat'})
        mold = next(row for row in result['rows'] if row['group'] == 'mold')
        self.assertEqual((mold['units'], mold['passed_units'], mold['rejected_units']),
                         ('100', '90', '10'))

    def test_source_coverage_reports_effective_contracts(self):
        self.register()
        result = self.explore.coverage('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(len(result['sources']), 2)
        self.assertEqual(result['sources'][0]['contract_windows'][0]['contract_id'], 'C_C')

    def test_explorer_rejects_unsupported_grouping(self):
        self.register()
        with self.assertRaises(Invalid):
            self.explore.summary('F1', '2026-02-01', '2026-02-02', 'payload')


class IntegrityAndOverviewTests(LakeCase):
    def test_integrity_passes_full_replay(self):
        self.published()
        result = self.integrity.inspect()
        self.assertTrue(result['valid'])
        self.assertEqual(result['counts']['accepted_events'], 2)
        self.assertEqual(result['counts']['release_rows'], 2)

    def test_integrity_detects_tampered_release_row(self):
        self.published()
        with self.storage.write() as db:
            db.execute('UPDATE release_rows SET data_json=? WHERE version=1 AND row_key='
                       '(SELECT min(row_key) FROM release_rows WHERE version=1)', ('{}',))
        result = self.integrity.inspect()
        self.assertFalse(result['valid'])
        self.assertTrue(any(item['code'] == 'release_rows_mismatch' for item in result['issues']))

    def test_facility_overview_shows_stage_totals(self):
        self.linked()
        result = self.overview.facility('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(result['material_link_count'], 1)
        self.assertEqual(result['event_count'], 2)
        mold = next(row for row in result['stages'] if row['stage'] == 'mold')
        self.assertEqual(mold['passed_units'], '90')

    def test_family_overview_shows_current_stage_per_lot(self):
        self.load()
        result = self.overview.family('LF1', '2026-02-01', '2026-02-02')
        self.assertEqual(result['event_count'], 2)
        self.assertEqual(len(result['lots']), 2)
        self.assertEqual({lot['last_stage'] for lot in result['lots']}, {'mold', 'coat'})

    def test_missing_release_fails_integrity_query(self):
        self.register()
        with self.assertRaises(Missing):
            self.audit.verify_release(1)
