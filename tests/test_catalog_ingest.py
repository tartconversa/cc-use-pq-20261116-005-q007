import copy

from optic_lake.errors import Conflict, Invalid, Missing
from tests.support import LakeCase


class CatalogTests(LakeCase):
    def test_fixture_registration_creates_expected_entities(self):
        self.register()
        state = self.storage.state()
        self.assertEqual(state['facilities'], 1)
        self.assertEqual(state['events'], 0)
        self.assertEqual(state['source_revision'], 8)
        self.assertEqual(len(self.catalog.list_contracts('SRC_M')), 1)

    def test_facility_duplicate_does_not_advance_revision(self):
        self.register()
        revision = self.storage.state()['source_revision']
        with self.assertRaises(Conflict):
            self.catalog.facility(**self.fixture['facilities'][0])
        self.assertEqual(self.storage.state()['source_revision'], revision)

    def test_closed_facility_rejects_unbounded_family(self):
        self.catalog.facility('F2', 'Closed', '2026-01-01', '2026-04-01')
        with self.assertRaises(Invalid):
            self.catalog.family('F2_F', 'F2', 'Family', '2026-02-01')

    def test_contract_overlap_is_rejected(self):
        self.register()
        item = copy.deepcopy(self.fixture['contracts'][0])
        item.update(contract_id='C_NEW', version=2, effective_day='2026-02-01')
        with self.assertRaises(Conflict):
            self.catalog.contract(**item)

    def test_adjacent_contract_windows_resolve_by_day(self):
        self.fixture['contracts'][0]['retired_day'] = '2026-03-01'
        self.register()
        item = copy.deepcopy(self.fixture['contracts'][0])
        item.update(contract_id='C_NEW', version=2,
                    effective_day='2026-03-01', retired_day=None)
        self.catalog.contract(**item)
        with self.storage.read() as db:
            self.assertEqual(self.catalog.resolve_contract(db, 'SRC_M', '2026-02-28')['contract_id'], 'C_M')
            self.assertEqual(self.catalog.resolve_contract(db, 'SRC_M', '2026-03-01')['contract_id'], 'C_NEW')

    def test_malformed_field_mapping_is_rejected(self):
        self.fixture['contracts'][0]['field_map']['station_id'] = 'lot_id'
        self.catalog.facility(**self.fixture['facilities'][0])
        self.catalog.source(**self.fixture['sources'][0])
        with self.assertRaises(Invalid):
            self.catalog.contract(**self.fixture['contracts'][0])

    def test_source_disable_is_idempotent_and_blocks_loading(self):
        self.register()
        first = self.catalog.enable_source('SRC_M', False)
        second = self.catalog.enable_source('SRC_M', False)
        self.assertEqual(first['revision'], second['revision'])
        with self.assertRaises(Invalid):
            self.ingest.submit(**self.fixture['batches'][0])
        self.catalog.enable_source('SRC_M', True)
        self.assertEqual(self.ingest.submit(**self.fixture['batches'][0])['accepted'], 1)

    def test_catalog_rejects_nonexistent_facility(self):
        with self.assertRaises(Missing):
            self.catalog.source('SRC_X', 'NO', 'mold')


class IngestionTests(LakeCase):
    def test_row_isolation_preserves_good_and_bad(self):
        self.register()
        result = self.ingest.submit(**self.fixture['batches'][0])
        self.assertEqual((result['accepted'], result['quarantined']), (1, 1))
        self.assertEqual(result['rows'][1]['outcome'], 'quarantined')
        self.assertIn('units must equal', result['rows'][1]['reason'])
        self.assertEqual(self.storage.state()['events'], 1)
        self.assertEqual(len(self.ingest.quarantine('B_M')), 1)

    def test_batch_retry_returns_original_result(self):
        self.register()
        first = self.ingest.submit(**self.fixture['batches'][0])
        revision = self.storage.state()['source_revision']
        second = self.ingest.submit(**self.fixture['batches'][0])
        self.assertEqual(first, second)
        self.assertEqual(self.storage.state()['source_revision'], revision)

    def test_batch_key_payload_conflict(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        altered = copy.deepcopy(self.fixture['batches'][0])
        altered['rows'][0]['passed_units'] = 88
        with self.assertRaises(Conflict):
            self.ingest.submit(**altered)

    def test_batch_id_conflict_for_different_source_key(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        altered = copy.deepcopy(self.fixture['batches'][0])
        altered['source_key'] = 'OTHER'
        with self.assertRaises(Conflict):
            self.ingest.submit(**altered)

    def test_existing_external_id_same_payload_is_duplicate(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        event = copy.deepcopy(self.fixture['batches'][0]['rows'][0])
        again = self.submit_one('DUP', event)
        self.assertEqual(again['duplicate'], 1)
        self.assertEqual(again['accepted'], 0)
        self.assertEqual(self.storage.state()['events'], 1)

    def test_existing_external_id_changed_payload_is_quarantined(self):
        self.register()
        self.ingest.submit(**self.fixture['batches'][0])
        changed = copy.deepcopy(self.fixture['batches'][0]['rows'][0])
        changed['units'] = 101
        changed['rejected_units'] = 11
        outcome = self.submit_one('CHANGED', changed)
        self.assertEqual(outcome['quarantined'], 1)
        self.assertIn('different content', outcome['rows'][0]['reason'])

    def test_repeated_external_id_within_batch_is_quarantined(self):
        self.register()
        event = self.new_event('E1')
        outcome = self.ingest.submit('B_REPEAT', 'SRC_M', 'REPEAT',
                                     '2026-02-03T09:00:00Z', [event, event])
        self.assertEqual((outcome['accepted'], outcome['quarantined']), (1, 1))
        self.assertEqual(self.storage.state()['events'], 1)

    def test_foreign_station_is_quarantined(self):
        self.register()
        event = self.new_event('WRONG', station='S_C')
        result = self.submit_one('WRONG', event)
        self.assertEqual(result['quarantined'], 1)
        self.assertIn('another stage', result['rows'][0]['reason'])

    def test_unknown_family_is_quarantined(self):
        self.register()
        event = self.new_event('UNKNOWN')
        event['family_id'] = 'MISSING'
        result = self.submit_one('UNKNOWN', event)
        self.assertEqual(result['quarantined'], 1)
        self.assertIn('not found', result['rows'][0]['reason'])

    def test_measurement_unit_normalizes_to_micrometers(self):
        self.load()
        event = self.ingest.event('SRC_M', 'M1')
        self.assertEqual(event['measured_unit'], 'um')
        self.assertEqual(event['measured_value'], '2.000')

    def test_unmapped_field_is_quarantined(self):
        self.register()
        event = self.new_event('UNKNOWN_FIELD')
        event['surprise'] = 3
        result = self.submit_one('UNKNOWN_FIELD', event)
        self.assertEqual(result['quarantined'], 1)
        self.assertIn('unmapped', result['rows'][0]['reason'])

    def test_negative_quantity_is_quarantined(self):
        self.register()
        event = self.new_event('NEGATIVE')
        event['rejected_units'] = -1
        result = self.submit_one('NEGATIVE', event)
        self.assertEqual(result['quarantined'], 1)

    def test_fractional_quantity_is_quarantined(self):
        self.register()
        event = self.new_event('FRACTION')
        event.update(units='1.5', passed_units='1.5', rejected_units=0)
        result = self.submit_one('FRACTION', event)
        self.assertEqual(result['quarantined'], 1)
        self.assertIn('whole lens', result['rows'][0]['reason'])

    def test_empty_event_identity_is_quarantined(self):
        self.register()
        event = self.new_event('')
        result = self.submit_one('EMPTY_ID', event)
        self.assertEqual(result['quarantined'], 1)

    def test_event_metadata_links_raw_batch_and_contract(self):
        self.load()
        event = self.ingest.event('SRC_M', 'M1')
        batch = self.ingest.batch('B_M')
        self.assertEqual(event['batch_id'], batch['batch_id'])
        self.assertEqual(event['contract_id'], batch['contract_id'])
        self.assertEqual(event['ordinal'], 0)

    def test_missing_batch_and_event_are_not_found(self):
        self.register()
        with self.assertRaises(Missing):
            self.ingest.batch('NONE')
        with self.assertRaises(Missing):
            self.ingest.event('SRC_M', 'NONE')
