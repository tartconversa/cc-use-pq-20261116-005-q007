import copy
import json
import tempfile
import unittest
from pathlib import Path

from optic_lake.audit import Audit
from optic_lake.catalog import Catalog
from optic_lake.explore import Explorer
from optic_lake.ingest import Ingestor
from optic_lake.integrity import Integrity
from optic_lake.lineage import Lineage
from optic_lake.overview import Overview
from optic_lake.quality import Quality
from optic_lake.snapshots import Snapshots
from optic_lake.storage import Storage


FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'demo.json').read_text(encoding='utf-8'))


class LakeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(str(Path(self.temp.name) / 'lake.sqlite3'))
        self.catalog = Catalog(self.storage)
        self.ingest = Ingestor(self.storage)
        self.lineage = Lineage(self.storage)
        self.snapshots = Snapshots(self.storage)
        self.quality = Quality(self.storage)
        self.audit = Audit(self.storage)
        self.explore = Explorer(self.storage)
        self.integrity = Integrity(self.storage)
        self.overview = Overview(self.storage)
        self.fixture = copy.deepcopy(FIXTURE)

    def register(self):
        for item in self.fixture['facilities']:
            self.catalog.facility(**item)
        for item in self.fixture['families']:
            self.catalog.family(**item)
        for item in self.fixture['stations']:
            self.catalog.station(**item)
        for item in self.fixture['sources']:
            self.catalog.source(**item)
        for item in self.fixture['contracts']:
            self.catalog.contract(**item)

    def load(self):
        self.register()
        return [self.ingest.submit(**batch) for batch in self.fixture['batches']]

    def linked(self):
        batches = self.load()
        links = [self.lineage.link(**item) for item in self.fixture['links']]
        return batches, links

    def published(self):
        self.linked()
        prepared = self.snapshots.prepare(**self.fixture['snapshot'])
        release = self.snapshots.publish(prepared['snapshot_id'])
        return prepared, release

    def new_event(self, name, lot='L_M', station='S_M', day='2026-02-01', units=10, passed=9):
        return {'external_id': name, 'family_id': 'LF1', 'lot_id': lot,
                'station_id': station, 'occurred_at': day + 'T10:00:00Z',
                'units': units, 'passed_units': passed,
                'rejected_units': units - passed}

    def submit_one(self, name, event, source='SRC_M'):
        return self.ingest.submit('B_' + name, source, 'FILE_' + name,
                                  '2026-02-03T09:00:00Z', [event])
