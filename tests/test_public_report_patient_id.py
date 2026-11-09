"""Public report ID regression checks using fake patients and temporary SQLite only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix="report-patient-id-")
os.environ.update({
    "DATABASE_URL": "sqlite:///" + (Path(TEMP.name) / "test.db").as_posix(),
    "ALLOW_SQLITE_FALLBACK": "false",
    "ENVIRONMENT": "development",
    "FILE_STORAGE_TRANSPORT": "local",
    "FILE_STORAGE_ROOT": str(Path(TEMP.name) / "files"),
    "JWT_SECRET": "isolated-report-patient-id-test-secret",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException, Response
from sqlalchemy.orm import Session
from app.database import Base, engine
from app.models import CaseDB, ReportDB, ReportPublicLinkDB, StudyDB
from app.routers.public_reports import get_public_report

assert engine.dialect.name == "sqlite"
assert str(Path(TEMP.name)).replace("\\", "/") in str(engine.url).replace("\\", "/")
Base.metadata.create_all(engine)


class PublicReportPatientIdTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)
        fields = dict(
            id="case-fixture", patient_number="SYSTEM-999", full_name="Fixture Patient",
            age=30, gender="Female", phone="", radiology_center_id="center-fixture",
            radiology_center_name="Fixture Centre", referring_physician_id="ref-fixture",
            referring_physician_name="Dr Fixture", status="Completed", study_date="2026-11-08",
            created_at="2026-11-08T00:00:00Z",
        )
        self.case = CaseDB(**fields, metadata_={})
        self.report = ReportDB(**fields, metadata_={})
        self.study = StudyDB(id="study-fixture", case_id=self.case.id, body_part="CHEST",
                             report_status="SIGNED", created_at=fields["created_at"],
                             signed_at=fields["created_at"], metadata_={})
        self.link = ReportPublicLinkDB(token="fixture-token", case_id=self.case.id,
                                       study_id=self.study.id, created_at=fields["created_at"])
        self.db.add_all([self.case, self.report, self.study, self.link])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def read(self):
        response = Response()
        result = get_public_report("fixture-token", response, self.db)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.case.patient_number, "SYSTEM-999")
        self.assertEqual(self.report.patient_number, "SYSTEM-999")
        return result["patient"]["patientId"]

    def test_report_metadata_takes_precedence(self):
        self.report.metadata_ = {"centerPatientId": " CTR-001 "}
        self.case.metadata_ = {"centerPatientId": "OTHER-CASE-ID"}
        self.assertEqual(self.read(), "CTR-001")

    def test_case_metadata_handles_legacy_and_blank_report_value(self):
        self.report.metadata_ = {"centerPatientId": "  "}
        self.case.metadata_ = {"centerPatientId": "000123"}
        self.assertEqual(self.read(), "000123")

    def test_missing_report_row_uses_case_id(self):
        self.db.delete(self.report)
        self.db.flush()
        self.case.metadata_ = {"centerPatientId": "CASE-123"}
        self.assertEqual(self.read(), "CASE-123")

    def test_missing_center_id_never_leaks_system_id(self):
        self.assertEqual(self.read(), "\u2014")

    def test_uuid_center_id_is_preserved(self):
        value = "8f3b2a1c-9d4e-4f5a-8b3c-1d2e3f4a5b6c"
        self.case.metadata_ = {"centerPatientId": value}
        self.assertEqual(self.read(), value)

    def test_revoked_link_remains_inaccessible(self):
        self.link.revoked_at = "2026-11-08T01:00:00Z"
        with self.assertRaises(HTTPException) as error:
            self.read()
        self.assertEqual(error.exception.status_code, 404)

    def test_unsigned_report_remains_inaccessible(self):
        self.study.report_status = "DRAFT"
        with self.assertRaises(HTTPException) as error:
            self.read()
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        engine.dispose()
        TEMP.cleanup()
