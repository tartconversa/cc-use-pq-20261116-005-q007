from optic_lake.errors import Conflict, Invalid, Missing, Stale
from tests.support import LakeCase


class LineageTests(LakeCase):
    def test_link_connects_two_lots_and_tracks_units(self):
        self.load()
        link = self.lineage.link(**self.fixture['links'][0])
        parent = self.lineage.lot('F1', 'L_M')
        child = self.lineage.lot('F1', 'L_C')
        self.assertEqual(link['link_id'], 'LINK1')
        self.assertEqual(parent['consumed_units'], '80')
        self.assertEqual(child['ancestors'], ['L_M'])
        self.assertEqual(child['inputs'][0]['used_units'], '80')

    def test_link_rejects_overconsumption(self):
        self.load()
        with self.assertRaises(Invalid):
            self.lineage.link('TOO_MUCH', 'F1', 'L_M', 'L_C', 91)
        self.assertEqual(self.lineage.lot('F1', 'L_M')['consumed_units'], '0')

    def test_multiple_links_cannot_exceed_shared_parent_output(self):
        self.load()
        self.lineage.link('FIRST', 'F1', 'L_M', 'L_C', 80)
        with self.assertRaises(Invalid):
            self.lineage.link('SECOND', 'F1', 'L_M', 'L_C', 11)
        self.assertEqual(self.lineage.lot('F1', 'L_M')['consumed_units'], '80')

    def test_reverse_link_cycle_is_rejected(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        self.submit_one('SECOND_LOT', self.new_event('SECOND_LOT', lot='L_SECOND', day='2026-02-01'))
        self.lineage.link('FORWARD', 'F1', 'L_M', 'L_SECOND', 10)
        with self.assertRaises(Conflict):
            self.lineage.link('REVERSE', 'F1', 'L_SECOND', 'L_M', 10)

    def test_self_link_is_rejected(self):
        self.load()
        with self.assertRaises(Invalid):
            self.lineage.link('SELF', 'F1', 'L_M', 'L_M', 5)

    def test_fractional_link_quantity_is_rejected(self):
        self.load()
        with self.assertRaises(Invalid):
            self.lineage.link('FRACTION', 'F1', 'L_M', 'L_C', '1.5')

    def test_missing_lot_is_not_found(self):
        self.register()
        with self.assertRaises(Missing):
            self.lineage.lot('F1', 'NO_LOT')

    def test_reconcile_contains_both_stages_and_lineage(self):
        self.linked()
        result = self.lineage.reconcile('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(len(result['rows']), 2)
        self.assertEqual(result['issues'], [])
        self.assertEqual(result['lineage_inputs'][0]['parents'][0]['parent_lot'], 'L_M')
        self.assertEqual({r['stage'] for r in result['rows']}, {'mold', 'coat'})

    def test_revision_window_omits_future_link(self):
        self.load()
        revision = self.storage.state()['source_revision']
        self.lineage.link('LATER', 'F1', 'L_M', 'L_C', 20)
        before = self.lineage.reconcile('F1', '2026-02-01', '2026-02-02', revision)
        after = self.lineage.reconcile('F1', '2026-02-01', '2026-02-02')
        self.assertEqual(before['lineage_inputs'], [])
        self.assertEqual(len(after['lineage_inputs']), 1)

    def test_reconcile_rejects_inverted_window(self):
        self.load()
        with self.assertRaises(Invalid):
            self.lineage.reconcile('F1', '2026-02-03', '2026-02-01')


class SnapshotTests(LakeCase):
    def test_prepare_and_publish_freezes_two_rows(self):
        prepared, release = self.published()
        self.assertEqual(prepared['row_count'], 2)
        self.assertEqual(release['version'], 1)
        self.assertEqual(len(self.snapshots.release(1)['rows']), 2)
        self.assertEqual(self.storage.state()['published_head'], 1)

    def test_publish_is_idempotent(self):
        _, release = self.published()
        again = self.snapshots.publish('SNAP1')
        self.assertEqual(release, again)
        self.assertEqual(self.storage.state()['published_head'], 1)

    def test_prepare_rejects_wrong_head(self):
        self.load()
        with self.assertRaises(Stale):
            self.snapshots.prepare('WRONG_HEAD', 'F1', '2026-02-01', '2026-02-02', 4)

    def test_prepare_rejects_empty_window(self):
        self.load()
        with self.assertRaises(Invalid):
            self.snapshots.prepare('EMPTY', 'F1', '2026-03-01', '2026-03-02')

    def test_source_change_stales_prepared_snapshot(self):
        self.load()
        prepared = self.snapshots.prepare('BEFORE', 'F1', '2026-02-01', '2026-02-02')
        self.submit_one('LATER', self.new_event('LATER'))
        with self.assertRaises(Stale):
            self.snapshots.publish(prepared['snapshot_id'])
        self.assertEqual(self.snapshots.snapshot('BEFORE')['state'], 'prepared')

    def test_two_prepared_snapshots_compete_for_head(self):
        self.load()
        self.snapshots.prepare('A', 'F1', '2026-02-01', '2026-02-02')
        self.snapshots.prepare('B', 'F1', '2026-02-01', '2026-02-02')
        self.snapshots.publish('A')
        with self.assertRaises(Stale):
            self.snapshots.publish('B')

    def test_new_snapshot_after_publish_gets_next_version(self):
        self.published()
        prepared = self.snapshots.prepare('NEXT', 'F1', '2026-02-01', '2026-02-02', 1)
        release = self.snapshots.publish(prepared['snapshot_id'])
        self.assertEqual(release['version'], 2)
        self.assertEqual(self.snapshots.latest()['version'], 2)

    def test_prepare_duplicate_snapshot_id_is_conflict(self):
        self.load()
        self.snapshots.prepare('ONCE', 'F1', '2026-02-01', '2026-02-02')
        with self.assertRaises(Conflict):
            self.snapshots.prepare('ONCE', 'F1', '2026-02-01', '2026-02-02')

    def test_release_lookup_rejects_unknown_version(self):
        self.load()
        with self.assertRaises(Missing):
            self.snapshots.release(1)

    def test_frozen_release_does_not_change_after_new_events(self):
        self.published()
        original = self.snapshots.release(1)
        self.submit_one('LATER', self.new_event('LATER'))
        self.assertEqual(self.snapshots.release(1), original)

    def test_reconcile_detects_overconsumed_historical_window(self):
        self.linked()
        historical = self.lineage.reconcile('F1', '2026-02-02', '2026-02-02')
        self.assertTrue(any(i['code'] == 'over_consumed' for i in historical['issues']))
        with self.assertRaises(Invalid):
            self.snapshots.prepare('PARTIAL', 'F1', '2026-02-02', '2026-02-02')
