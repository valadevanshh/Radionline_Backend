"""PACS workflow regression tests: fake records and temporary SQLite, never production."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest

TEMP = tempfile.TemporaryDirectory(prefix="pacs-workflow-")
os.environ.update({
    "DATABASE_URL": "sqlite:///" + (Path(TEMP.name) / "test.db").as_posix(),
    "ALLOW_SQLITE_FALLBACK": "false", "ENVIRONMENT": "development",
    "FILE_STORAGE_TRANSPORT": "local", "FILE_STORAGE_ROOT": str(Path(TEMP.name) / "files"),
    "JWT_SECRET": "isolated-pacs-workflow-test-secret",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy.orm import Session
from app.database import Base, engine
from app.models import CaseDB, StudyDB, TemplateDB, UserDB
from app.schemas import TemplateCreate
from app.routers.study_reports import StudyReportPayload, save_study_draft, sign_study_report
from app.routers.templates import save_template_core
assert engine.dialect.name == "sqlite"
assert str(Path(TEMP.name)).replace("\\", "/") in str(engine.url).replace("\\", "/")
Base.metadata.create_all(engine)

class PacsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.user = UserDB(email="doctor-a@example.invalid", name="Doctor A", role="DOCTOR", metadata_={"doctorId": "doctor-a"})
        self.db.add(self.user)
        self.cases = []
        for suffix in ["a", "b"]:
            case = CaseDB(id="case-"+suffix, patient_number="SYSTEM-"+suffix, full_name="Sample "+suffix,
                          age=35, gender="Female", phone="", radiology_center_id="center-a",
                          radiology_center_name="Sample Center", referring_physician_id="ref-a",
                          referring_physician_name="Sample Referrer", status="In Review",
                          study_date="2026-11-08", created_at="2026-11-08", metadata_={})
            self.db.add(case)
            self.cases.append(case)
        self.study = StudyDB(id="study-a", case_id="case-a", body_part="CHEST PA", modality="X-Ray",
                             claimed_by="doctor-a", status="CLAIMED", report_status="DRAFT",
                             findings="Original template findings", impression="Original impression",
                             created_at="2026-11-08", metadata_={})
        self.other_study = StudyDB(id="study-a2", case_id="case-a", body_part="KNEE AP", modality="X-Ray",
                                   claimed_by="doctor-a", status="CLAIMED", report_status="DRAFT",
                                   findings="Other body part unchanged", created_at="2026-11-08", metadata_={})
        self.other_patient = StudyDB(id="study-b", case_id="case-b", body_part="CHEST PA", modality="X-Ray",
                                     claimed_by="doctor-b", status="CLAIMED", report_status="DRAFT",
                                     findings="Other patient unchanged", created_at="2026-11-08", metadata_={})
        self.template = TemplateDB(id="template-baseline", title="Normal chest", center_id="ALL", center_name="All Centers",
                                   modality="X-Ray", created_at="2026-11-08", metadata_={"findings":"Original template findings", "doctorId":"doctor-a"})
        self.db.add_all([self.study, self.other_study, self.other_patient, self.template])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def draft(self, study=None, **kwargs):
        target = study or self.study
        return asyncio.run(save_study_draft(target.case_id, target.id, StudyReportPayload(**kwargs), self.db, self.user))

    def test_patient_edits_leave_template_other_study_and_other_patient_unchanged(self):
        result = self.draft(findings="Patient A addition", impression="Patient A revised impression", title="Patient-specific title", templateId="template-baseline")
        self.assertTrue(result["ok"])
        self.assertEqual(self.study.findings, "Patient A addition")
        self.assertEqual(self.study.template_id, "template-baseline")
        self.assertEqual(self.study.metadata_["title"], "Patient-specific title")
        self.assertEqual(self.template.metadata_["findings"], "Original template findings")
        self.assertEqual(self.other_study.findings, "Other body part unchanged")
        self.assertEqual(self.other_patient.findings, "Other patient unchanged")
        self.assertEqual(self.cases[0].metadata_["reportsByBodyPart"]["CHEST PA"], "Patient A addition")
        saved = next(s for s in result["report"]["studies"] if s["id"] == "study-a")
        self.assertEqual(saved["title"], "Patient-specific title")

    def test_doctor_can_remove_text_from_own_draft(self):
        self.draft(findings="", impression="Remaining impression")
        self.assertEqual(self.study.findings, "")
        self.assertNotIn("CHEST PA", self.cases[0].metadata_["reportsByBodyPart"])

    def test_other_doctors_patient_is_denied(self):
        with self.assertRaises(HTTPException) as error:
            self.draft(self.other_patient, findings="Forbidden")
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.other_patient.findings, "Other patient unchanged")

    def test_unclaimed_study_is_denied(self):
        self.study.claimed_by = None
        with self.assertRaises(HTTPException) as error:
            self.draft(findings="Forbidden")
        self.assertEqual(error.exception.status_code, 403)

    def test_signed_study_in_partial_case_cannot_be_drafted_or_resigned(self):
        self.study.report_status = "SIGNED"
        with self.assertRaises(HTTPException) as error:
            self.draft(findings="Forbidden")
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(HTTPException) as error:
            asyncio.run(sign_study_report("case-a", "study-a", StudyReportPayload(findings="Forbidden"), self.db, self.user))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.study.findings, "Original template findings")

    def test_completed_case_is_read_only(self):
        self.cases[0].status = "Completed"
        with self.assertRaises(HTTPException) as error:
            self.draft(findings="Forbidden")
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.study.findings, "Original template findings")

    def test_new_template_is_private_and_does_not_change_patient(self):
        result = save_template_core(TemplateCreate(id="template-new", title="New knee view", modality="X-Ray", bodyPart="KNEE AP", findings="Reusable knee findings"), self.db, self.user)
        self.assertEqual(result["doctorId"], "doctor-a")
        self.assertEqual(result["bodyPart"], "KNEE AP")
        self.assertEqual(self.study.findings, "Original template findings")
        self.assertEqual(self.template.metadata_["findings"], "Original template findings")

if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        engine.dispose()
        TEMP.cleanup()
