"""Isolated SQLite/file tests; never load remote database or SFTP settings."""
import os, sys, tempfile, unittest, base64
from pathlib import Path
from unittest.mock import patch
TEMP = tempfile.TemporaryDirectory(prefix="profile-documents-")
os.environ.update(DATABASE_URL="sqlite:///"+(Path(TEMP.name)/"test.db").as_posix(), ALLOW_SQLITE_FALLBACK="false",
                  ENVIRONMENT="development", FILE_STORAGE_TRANSPORT="local", FILE_STORAGE_ROOT=str(Path(TEMP.name)/"files"), JWT_SECRET="isolated-document-test-secret")
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from app.database import Base, engine, get_db
from app.models import DoctorDB, CenterDB, UserDB
from app.security import get_current_user
from app.routers import doctors, centers
from app.routers.approvals import _apply_center_action, _apply_doctor_action
from app.profile_documents import prepare_documents, ProfileDocument, MAX_FILES, MAX_BYTES
assert engine.dialect.name == "sqlite" and TEMP.name.replace("\\","/") in str(engine.url).replace("\\","/")
Base.metadata.create_all(engine)

def upload(name="certificate.pdf",mime="application/pdf",content=b"%PDF-1.4 sample"):
    return {"fileName":name,"url":"data:"+mime+";base64,"+base64.b64encode(content).decode()}

class ProfileDocumentTests(unittest.TestCase):
    def setUp(self):
        self.db=Session(engine)
        for model in [UserDB,DoctorDB,CenterDB]: self.db.query(model).delete()
        self.doc=DoctorDB(id="doc-a",first_name="Sample",last_name="Doctor",full_name="Dr Sample Doctor",email="doctor@example.invalid",contact_number="123",created_at="2026-11-08",metadata_={"cvUrl":"2026/11/08/doctors/doc-a/cv/old.pdf","signatureScale":125})
        self.center=CenterDB(id="center-a",center_name="Sample Center",email="center@example.invalid",contact_number="123",created_at="2026-11-08",metadata_={"letterheadMode":"header","referringDoctors":[{"name":"Sample"}]})
        self.user=UserDB(email="doctor@example.invalid",name="Sample Doctor",role="DOCTOR",metadata_={"doctorId":"doc-a"})
        self.db.add_all([self.doc,self.center,self.user]);self.db.commit()
        app=FastAPI();app.include_router(doctors.router);app.include_router(centers.router)
        app.dependency_overrides[get_db]=lambda:self.db
        app.dependency_overrides[get_current_user]=lambda:self.user
        self.client=TestClient(app)
    def tearDown(self): self.client.close();self.db.close()
    def put_doctor(self,**body):return self.client.put("/doctors/me/profile",json=body)
    def test_doctor_multi_upload_reload_append_remove_and_clear_preserves_cv_signature(self):
        result=self.put_doctor(certificates=[upload(),upload("registration.png","image/png",b"PNG sample")]);self.assertEqual(result.status_code,200,result.text)
        saved=result.json()["certificates"];self.assertEqual(len(saved),2);self.assertNotEqual(saved[0]["id"],saved[1]["id"])
        self.assertFalse(saved[0]["url"].startswith("data:"))
        self.assertEqual(self.client.get("/doctors/me").json()["certificates"],saved)
        result=self.put_doctor(certificates=[*saved,upload("qualification.docx","application/vnd.openxmlformats-officedocument.wordprocessingml.document")]);self.assertEqual(len(result.json()["certificates"]),3)
        self.assertEqual(self.put_doctor(certificates=[saved[1]]).json()["certificates"],[saved[1]])
        self.assertEqual(self.put_doctor(degree="MD").json()["certificates"],[saved[1]])
        result=self.put_doctor(certificates=[]).json();self.assertEqual(result["certificates"],[]);self.assertEqual(result["signatureScale"],125);self.assertTrue(result["cvUrl"].endswith("old.pdf"))
    def test_invalid_batch_creates_no_files_and_changes_no_metadata(self):
        original=dict(self.doc.metadata_)
        with patch("app.profile_documents.storage.save_bytes") as save:
            result=self.put_doctor(certificates=[upload(),upload("bad.exe","application/x-msdownload")])
            self.assertEqual(result.status_code,400,result.text);save.assert_not_called()
        self.db.refresh(self.doc);self.assertEqual(self.doc.metadata_,original)
    def test_arbitrary_or_other_profile_reference_is_denied(self):
        result=self.put_doctor(certificates=[{"id":"other","fileName":"other.pdf","url":"https://example.invalid/other.pdf"}])
        self.assertEqual(result.status_code,400)
    def test_invalid_empty_oversized_and_too_many_documents(self):
        for values in [[upload(content=b"")],[upload(content=b"x"*(MAX_BYTES+1))],[upload() for _ in range(MAX_FILES+1)],[{"fileName":"bad.pdf","url":"data:application/pdf;base64,!!!!"}]]:
            with self.assertRaises(HTTPException):prepare_documents(values)
    def test_duplicate_saved_reference_denied(self):
        saved=self.put_doctor(certificates=[upload()]).json()["certificates"]
        self.assertEqual(self.put_doctor(certificates=saved+saved).status_code,400)
    def test_doctor_cannot_edit_another_profile(self):
        self.assertEqual(self.client.put("/doctors/doc-b/profile",json={"certificates":[upload()]}).status_code,403)
    def test_center_own_multi_documents_and_other_center_denied(self):
        self.user.role="CENTER";self.user.metadata_={"centerId":"center-a"};self.db.commit()
        result=self.client.put("/centers/center-a/profile",json={"documents":[upload("license.pdf"),upload("registration.pdf")]})
        self.assertEqual(result.status_code,200,result.text);saved=result.json()["documents"];self.assertEqual(len(saved),2)
        self.assertEqual(result.json()["letterheadMode"],"header")
        self.assertEqual(self.client.put("/centers/center-b/profile",json={"documents":saved}).status_code,403)
        self.assertEqual(self.client.put("/centers/center-a/profile",json={"documents":[]}).json()["documents"],[])
        self.db.refresh(self.center);self.assertEqual(self.center.metadata_["referringDoctors"],[{"name":"Sample"}])
    def test_manager_approval_persists_documents_and_preserves_existing_center_metadata(self):
        payload={"centerName":"Sample Center","documents":[upload("license.pdf")]}
        _apply_center_action("UPDATE_CENTER","center-a",payload,self.db);self.db.commit();self.db.refresh(self.center)
        self.assertEqual(len(self.center.metadata_["documents"]),1);self.assertEqual(self.center.metadata_["letterheadMode"],"header")
        _apply_doctor_action("UPDATE_DOCTOR","doc-a",{"certificates":[upload()]},self.db);self.db.commit();self.db.refresh(self.doc)
        self.assertEqual(len(self.doc.metadata_["certificates"]),1);self.assertEqual(self.doc.metadata_["signatureScale"],125)
    def test_admin_creation_saves_both_document_lists(self):
        self.user.role="SUPER_ADMIN";self.db.commit()
        doctor_payload={"id":"doc-new","firstName":"New","lastName":"Doctor","fullName":"Dr New Doctor","email":"new@example.invalid","contactNumber":"123","certificates":[upload(),upload("license.pdf")]}
        result=self.client.post("/doctors",json=doctor_payload)
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(len(result.json()["certificates"]),2)
        center_payload={"id":"center-new","centerName":"New Center","email":"new-center@example.invalid","contactNumber":"123","documents":[upload(),upload("license.pdf")]}
        result=self.client.post("/centers",json=center_payload)
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(len(result.json()["documents"]),2)

    def test_legacy_profile_returns_empty_document_lists(self):
        self.assertEqual(doctors.db_to_schema(self.doc)["certificates"],[]);self.assertEqual(centers.db_to_schema(self.center)["documents"],[])

def tearDownModule():
    engine.dispose()
    TEMP.cleanup()

if __name__=="__main__":unittest.main()
