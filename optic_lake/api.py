import os
import inspect
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .catalog import Catalog
from .audit import Audit
from .errors import Invalid, LakeError
from .explore import Explorer
from .ingest import Ingestor
from .integrity import Integrity
from .lineage import Lineage
from .overview import Overview
from .quality import Quality
from .snapshots import Snapshots
from .storage import Storage


def create_app(db_path='optic-lake.sqlite3'):
    storage = Storage(db_path)
    catalog = Catalog(storage)
    ingest = Ingestor(storage)
    lineage = Lineage(storage)
    quality = Quality(storage)
    snapshots = Snapshots(storage)
    audit = Audit(storage)
    explore = Explorer(storage)
    integrity = Integrity(storage)
    overview = Overview(storage)
    app = FastAPI(title='Optical Manufacturing Lake', version='1.0.0')

    def apply(fn, data):
        if not isinstance(data, dict):
            raise Invalid('request body must be an object')
        signature = inspect.signature(fn)
        required = {name for name, param in signature.parameters.items()
                    if param.default is inspect.Parameter.empty}
        allowed = set(signature.parameters)
        if required - set(data) or set(data) - allowed:
            raise Invalid('request fields do not match operation',
                          missing=sorted(required - set(data)),
                          unknown=sorted(set(data) - allowed))
        return fn(**data)

    @app.exception_handler(LakeError)
    async def lake_error(request: Request, exc: LakeError):
        return JSONResponse(exc.json(), status_code=exc.status)

    @app.get('/health')
    def health():
        return storage.state()

    @app.get('/integrity')
    def inspect_integrity():
        return integrity.inspect()

    @app.get('/overview/facilities/{facility_id}')
    def facility_overview(facility_id: str, from_day: str, to_day: str):
        return overview.facility(facility_id, from_day, to_day)

    @app.get('/overview/families/{family_id}')
    def family_overview(family_id: str, from_day: str, to_day: str):
        return overview.family(family_id, from_day, to_day)

    @app.post('/facilities', status_code=201)
    def create_facility(data: dict):
        return apply(catalog.facility, data)

    @app.post('/families', status_code=201)
    def create_family(data: dict):
        return apply(catalog.family, data)

    @app.post('/stations', status_code=201)
    def create_station(data: dict):
        return apply(catalog.station, data)

    @app.post('/sources', status_code=201)
    def create_source(data: dict):
        return apply(catalog.source, data)

    @app.put('/sources/{source_id}/enabled')
    def enable_source(source_id: str, data: dict):
        return apply(lambda enabled: catalog.enable_source(source_id, enabled), data)

    @app.post('/contracts', status_code=201)
    def create_contract(data: dict):
        return apply(catalog.contract, data)

    @app.get('/sources/{source_id}/contracts')
    def list_contracts(source_id: str):
        return catalog.list_contracts(source_id)

    @app.post('/batches', status_code=201)
    def submit_batch(data: dict):
        return apply(ingest.submit, data)

    @app.get('/batches/{batch_id}')
    def get_batch(batch_id: str):
        return ingest.batch(batch_id)

    @app.get('/batches/{batch_id}/quarantine')
    def get_quarantine(batch_id: str):
        return ingest.quarantine(batch_id)

    @app.get('/events/{source_id}/{external_id}')
    def get_event(source_id: str, external_id: str):
        return ingest.event(source_id, external_id)

    @app.post('/links', status_code=201)
    def create_link(data: dict):
        return apply(lineage.link, data)

    @app.get('/lots/{facility_id}/{lot_id}')
    def get_lot(facility_id: str, lot_id: str):
        return lineage.lot(facility_id, lot_id)

    @app.get('/reconcile/{facility_id}')
    def reconcile(facility_id: str, from_day: str, to_day: str, source_revision: int | None = None):
        return lineage.reconcile(facility_id, from_day, to_day, source_revision)

    @app.get('/quality/{facility_id}')
    def quality_report(facility_id: str, from_day: str, to_day: str,
                       source_revision: int | None = None):
        return quality.report(facility_id, from_day, to_day, source_revision)

    @app.get('/sources/{source_id}/detail')
    def source_detail(source_id: str):
        return quality.source_detail(source_id)

    @app.post('/snapshots', status_code=201)
    def prepare_snapshot(data: dict):
        return apply(snapshots.prepare, data)

    @app.get('/snapshots/{snapshot_id}')
    def get_snapshot(snapshot_id: str):
        return snapshots.snapshot(snapshot_id)

    @app.post('/snapshots/{snapshot_id}/publish')
    def publish_snapshot(snapshot_id: str):
        return snapshots.publish(snapshot_id)

    @app.get('/releases/latest')
    def latest_release():
        return snapshots.latest()

    @app.get('/explore/events/{facility_id}')
    def explore_events(facility_id: str, from_day: str, to_day: str,
                       family_id: str | None = None, lot_id: str | None = None,
                       process_stage: str | None = None, source_id: str | None = None,
                       after_revision: int = 0, limit: int = 100):
        return explore.events(facility_id, from_day, to_day, family_id=family_id,
                              lot_id=lot_id, process_stage=process_stage,
                              source_id=source_id, after_revision=after_revision, limit=limit)

    @app.get('/explore/releases/{version}/rows')
    def explore_release(version: int, family_id: str | None = None,
                        lot_id: str | None = None, process_stage: str | None = None,
                        from_day: str | None = None, to_day: str | None = None,
                        limit: int = 500):
        return explore.release_rows(version, family_id=family_id, lot_id=lot_id,
                                    process_stage=process_stage, from_day=from_day,
                                    to_day=to_day, limit=limit)

    @app.get('/explore/summary/{facility_id}')
    def explore_summary(facility_id: str, from_day: str, to_day: str,
                        group_by: str = 'stage', source_revision: int | None = None):
        return explore.summary(facility_id, from_day, to_day, group_by, source_revision)

    @app.get('/explore/coverage/{facility_id}')
    def explore_coverage(facility_id: str, from_day: str, to_day: str):
        return explore.coverage(facility_id, from_day, to_day)

    @app.get('/releases/{version}')
    def get_release(version: int):
        return snapshots.release(version)

    @app.get('/audit/timeline')
    def audit_timeline(from_revision: int = 1, to_revision: int | None = None, limit: int = 500):
        return audit.timeline(from_revision, to_revision, limit)

    @app.get('/audit/lots/{facility_id}/{lot_id}')
    def audit_lot(facility_id: str, lot_id: str):
        return audit.event_history(facility_id, lot_id)

    @app.get('/audit/batches/{source_id}')
    def audit_batches(source_id: str, from_day: str, to_day: str):
        return audit.batch_history(source_id, from_day, to_day)

    @app.get('/audit/releases/{version}/verify')
    def verify_release(version: int):
        return audit.verify_release(version)

    @app.get('/audit/releases/compare')
    def compare_releases(left_version: int, right_version: int):
        return audit.compare_releases(left_version, right_version)

    return app


app = create_app(os.environ.get('OPTIC_LAKE_DB', 'optic-lake.sqlite3'))
