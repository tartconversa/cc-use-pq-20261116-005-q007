import argparse
import json
import tempfile
from pathlib import Path

from .catalog import Catalog
from .ingest import Ingestor
from .lineage import Lineage
from .snapshots import Snapshots
from .storage import Storage


def replay(fixture, db_path):
    data = json.loads(Path(fixture).read_text(encoding='utf-8'))
    storage = Storage(db_path)
    catalog, ingest = Catalog(storage), Ingestor(storage)
    lineage, snapshots = Lineage(storage), Snapshots(storage)
    for item in data['facilities']:
        catalog.facility(**item)
    for item in data['families']:
        catalog.family(**item)
    for item in data['stations']:
        catalog.station(**item)
    for item in data['sources']:
        catalog.source(**item)
    for item in data['contracts']:
        catalog.contract(**item)
    batches = [ingest.submit(**item) for item in data['batches']]
    links = [lineage.link(**item) for item in data['links']]
    recon = lineage.reconcile(data['snapshot']['facility_id'],
                              data['snapshot']['from_day'], data['snapshot']['to_day'])
    prepared = snapshots.prepare(**data['snapshot'])
    published = snapshots.publish(data['snapshot']['snapshot_id'])
    release = snapshots.release(published['version'])
    return {'batches': batches, 'links': links, 'reconcile': recon,
            'snapshot': prepared, 'release': release}


def main():
    parser = argparse.ArgumentParser(description='Replay a local optical manufacturing fixture')
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--db')
    args = parser.parse_args()
    if args.db:
        outcome = replay(args.fixture, args.db)
    else:
        with tempfile.TemporaryDirectory() as folder:
            outcome = replay(args.fixture, str(Path(folder) / 'demo.sqlite3'))
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
