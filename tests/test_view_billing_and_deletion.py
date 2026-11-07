"""Isolated SQLite tests: view-count billing and deleted-patient invoices.

Uses fake patients only. Never connects to Postgres (unreachable DATABASE_URL).
"""
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="viewbill_")
os.environ["DATABASE_URL"] = "postgresql://nobody:nothing@127.0.0.1:1/none"
os.environ["ALLOW_SQLITE_FALLBACK"] = "true"
os.environ["ENVIRONMENT"] = "development"
os.environ["FILE_STORAGE_TRANSPORT"] = "local"
os.environ["FILE_STORAGE_ROOT"] = os.path.join(TMP, "files")
os.environ["JWT_SECRET"] = "view-billing-test-secret"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(TMP)

from app.database import engine, Base, SessionLocal  # noqa: E402

assert engine.dialect.name == "sqlite", engine.url
import app.models  # noqa: E402,F401

Base.metadata.create_all(bind=engine)
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.models import (  # noqa: E402
    CaseDB,
    InvoiceDB,
    InvoiceLineItemDB,
    StudyImageDB,
    UserDB,
)
from app.security import hash_password  # noqa: E402
from app.view_billing import billable_view_count  # noqa: E402
from app.billing import (  # noqa: E402
    preview_open_invoice_deletion_adjustments,
    preview_view_count_deltas,
)

c = TestClient(app)
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:500]) if not cond else ""))


db = SessionLocal()
if not db.query(UserDB).filter(UserDB.email == "sa@view.test").first():
    db.add(UserDB(
        email="sa@view.test",
        name="View SA",
        role="SUPER_ADMIN",
        metadata_={"password": hash_password("SaPass123!")},
    ))
    db.commit()
db.close()


def login(email, password):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": "Bearer " + body["access_token"]}, body["user"]


SA, _ = login("sa@view.test", "SaPass123!")


def pure_view_counts():
    check("one view label", billable_view_count(body_part="CHEST PA") == 1)
    check("AP SUPINE is one view", billable_view_count(body_part="CHEST AP SUPINE") == 1)
    check("AP/LATERAL is two views", billable_view_count(body_part="KNEE AP/LATERAL") == 2)
    check("AP/LAT matches AP/LATERAL", billable_view_count(body_part="FOREARM AP/LAT") == 2)
    check("side suffix ignored", billable_view_count(body_part="FINGER AP (RIGHT)") == 1)
    check("PA & LATERAL is two", billable_view_count(body_part="CHEST PA & LATERAL") == 2)
    check("no view metadata is one unit", billable_view_count(body_part="CUSTOM PROCEDURE") == 1)
    check("unknown second token is not guessed", billable_view_count(body_part="KNEE AP/MYSTERY") == 1)
    check(
        "explicit two views",
        billable_view_count(body_part="KNEE", explicit_views=["AP", "LATERAL"]) == 2,
    )
    check(
        "LAT and LATERAL are one view",
        billable_view_count(body_part="KNEE", explicit_views=["LAT", "LATERAL"]) == 1,
    )
    check(
        "explicit list beats a one-view name",
        billable_view_count(body_part="CHEST PA", explicit_views=["AP", "LATERAL"]) == 2,
    )
    # The helper has no image argument; a large file count cannot be passed in.
    check("function does not accept an image count", "image" not in billable_view_count.__code__.co_varnames)


def make_center(name, email, password):
    r = c.post("/api/centers", headers=SA, json={
        "centerName": name,
        "email": email,
        "contactNumber": "9000000001",
        "username": email,
        "password": password,
    })
    check(f"create centre {name}", r.status_code == 200, r.text)
    return r.json()["id"]


def make_doctor(name, email, password):
    first, last = name.split(" ", 1)
    r = c.post("/api/doctors", headers=SA, json={
        "firstName": first,
        "lastName": last,
        "fullName": name,
        "email": email,
        "contactNumber": "9000000002",
        "username": email,
        "password": password,
    })
    check(f"create doctor {name}", r.status_code == 200, r.text)
    return r.json()["id"]


def upload(center_id, doctor_headers, full_name, studies):
    r = c.post("/api/reports", headers=SA, json={
        "fullName": full_name,
        "patientNumber": "",
        "age": 40,
        "gender": "Female",
        "phone": "9000000099",
        "radiologyCenterId": center_id,
        "radiologyCenterName": "ignored",
        "referringPhysicianId": "ref-1",
        "referringPhysicianName": "REFERRER TEST",
        "assignedDoctorIds": ["ALL"],
        "status": "Pending",
        "studyDate": "07-11-2026",
        "modality": "X-Ray",
        "bodyParts": [s["bodyPart"] for s in studies],
        "studyUploads": studies,
    })
    check(f"upload {full_name}", r.status_code == 200, r.text)
    body = r.json()
    case_id = body["id"]
    r = c.post(f"/api/reports/{case_id}/claim", headers=doctor_headers, json={
        "doctorId": "ignored",
        "doctorName": "ignored",
    })
    check(f"claim {full_name}", r.status_code == 200, r.text)
    loaded = c.get(f"/api/reports/{case_id}", headers=SA)
    check(f"load {full_name}", loaded.status_code == 200, loaded.text)
    return loaded.json()


def add_images(study_id, n):
    db = SessionLocal()
    for i in range(n):
        db.add(StudyImageDB(
            id=f"img-{study_id}-{i}",
            study_id=study_id,
            image_url=f"mem://fake/{study_id}/{i}",
            image_type="uploaded",
            created_at="2026-11-07T00:00:00Z",
        ))
    db.commit()
    count = db.query(StudyImageDB).filter(StudyImageDB.study_id == study_id).count()
    db.close()
    return count


def sign_all(doctor_headers, report):
    for study in report.get("studies") or []:
        r = c.post(
            f"/api/reports/{report['id']}/studies/{study['id']}/sign",
            headers=doctor_headers,
            json={"findings": "No acute fake finding.", "impression": "Normal fake study."},
        )
        check(f"sign {study.get('bodyPart')}", r.status_code == 200, r.text)
        if r.status_code == 200 and r.json().get("caseComplete"):
            return r.json()
    return None


def doctor_invoice(headers):
    listed = c.get("/api/invoices", headers=headers, params={"partyType": "doctor"})
    check("doctor invoice list", listed.status_code == 200, listed.text)
    rows = listed.json()
    if not rows:
        return None
    detail = c.get(f"/api/invoices/{rows[0]['id']}", headers=headers)
    check("doctor invoice detail", detail.status_code == 200, detail.text)
    return detail.json()


def center_invoice_for(center_id):
    listed = c.get("/api/invoices", headers=SA, params={"partyType": "center", "partyId": center_id})
    rows = listed.json()
    if not rows:
        return None
    return c.get(f"/api/invoices/{rows[0]['id']}", headers=SA).json()


def line_for(inv, name):
    for li in inv.get("lineItems") or []:
        if li.get("patientName") == name:
            return li
    return None


def totals_consistent(inv, label):
    items = inv.get("lineItems") or []
    total = int(inv.get("totalAmount") or 0)
    check(f"{label} lines sum to total", sum(int(li["amount"]) for li in items) == total, inv)
    days = inv.get("byDay") or []
    if days:
        check(
            f"{label} day totals sum",
            sum(int(d["dayTotal"]) for d in days) == total,
            days,
        )
    centers = inv.get("byCenter") or []
    if centers:
        check(
            f"{label} centre totals sum",
            sum(int(x["centerTotal"]) for x in centers) == total,
            centers,
        )


pure_view_counts()

north = make_center("North Test Centre", "north@view.test", "North123!")
south = make_center("South Test Centre", "south@view.test", "South123!")
rated = make_center("Rated Test Centre", "rated@view.test", "Rated123!")
make_doctor("Ada Ray", "ada@view.test", "AdaRay123!")
make_doctor("Bea Ray", "bea@view.test", "BeaRay123!")
make_doctor("Cara Ray", "cara@view.test", "CaraRay123!")
ADA, _ = login("ada@view.test", "AdaRay123!")
BEA, _ = login("bea@view.test", "BeaRay123!")
CARA, _ = login("cara@view.test", "CaraRay123!")
NORTH, _ = login("north@view.test", "North123!")
SOUTH, _ = login("south@view.test", "South123!")

r = c.put(f"/api/billing/center-pricing/{rated}", headers=SA, json={
    "centerFirstStudy": 50,
    "centerAdditionalStudy": 9,
    "doctorFirstStudy": 40,
    "doctorAdditionalStudy": 7,
})
check("custom tariff saved", r.status_code == 200, r.text)

# 1 explicit view, many image files.
one = upload(north, ADA, "FAKE PATIENT ONE", [{
    "bodyPart": "CHEST PA",
    "views": ["PA"],
    "images": [],
    "studyDate": "07-11-2026",
}])
imgs = add_images(one["studies"][0]["id"], 6)
check("six image files stored", imgs == 6)
sign_all(ADA, one)

# 2 explicit views, one image file.
two = upload(north, ADA, "FAKE PATIENT TWO", [{
    "bodyPart": "KNEE AP/LATERAL",
    "views": ["AP", "LATERAL"],
    "images": [],
    "studyDate": "07-11-2026",
}])
imgs = add_images(two["studies"][0]["id"], 1)
check("one image file stored for two views", imgs == 1)
sign_all(ADA, two)

# Historical name only, no views array. AP/LATERAL must still be 2.
hist = upload(south, ADA, "FAKE PATIENT HIST", [{
    "bodyPart": "FOREARM AP/LATERAL",
    "images": [],
    "studyDate": "07-11-2026",
}])
add_images(hist["studies"][0]["id"], 4)
sign_all(ADA, hist)

# Missing view metadata: one unit, not the file count.
missing = upload(south, ADA, "FAKE PATIENT PLAIN", [{
    "bodyPart": "CUSTOM PROCEDURE",
    "images": [],
    "studyDate": "07-11-2026",
}])
add_images(missing["studies"][0]["id"], 5)
sign_all(ADA, missing)

# Two studies in one case: 1 view then 2 views. Second study is not priced as a single additional study.
combo = upload(north, ADA, "FAKE PATIENT COMBO", [
    {"bodyPart": "CHEST AP SUPINE", "views": ["AP SUPINE"], "images": [], "studyDate": "07-11-2026"},
    {"bodyPart": "LUMBAR SPINE AP/LAT", "views": ["AP", "LAT"], "images": [], "studyDate": "07-11-2026"},
])
add_images(combo["studies"][0]["id"], 3)
add_images(combo["studies"][1]["id"], 3)
signed = sign_all(ADA, combo)
check("combo billing viewCount is 3", (signed or {}).get("billing", {}).get("viewCount") == 3, signed)

# Custom rate card: 2 views => first + additional, not 2x first.
rated_case = upload(rated, ADA, "FAKE PATIENT RATED", [{
    "bodyPart": "KNEE",
    "views": ["AP", "LATERAL"],
    "images": [],
    "studyDate": "07-11-2026",
}])
sign_all(ADA, rated_case)

inv = doctor_invoice(ADA)
check("doctor invoice loaded", inv is not None)
if inv:
    totals_consistent(inv, "doctor")
    one_line = line_for(inv, "FAKE PATIENT ONE")
    two_line = line_for(inv, "FAKE PATIENT TWO")
    hist_line = line_for(inv, "FAKE PATIENT HIST")
    plain_line = line_for(inv, "FAKE PATIENT PLAIN")
    combo_line_chest = None
    combo_line_spine = None
    for li in inv["lineItems"]:
        if li["patientName"] != "FAKE PATIENT COMBO":
            continue
        if "SUPINE" in (li.get("bodyPart") or ""):
            combo_line_chest = li
        if "LUMBAR" in (li.get("bodyPart") or ""):
            combo_line_spine = li
    rated_line = line_for(inv, "FAKE PATIENT RATED")
    check("1 view 6 files pays first rate", one_line and one_line["amount"] == 30 and one_line["quantity"] == 1, one_line)
    check("2 views 1 file pays first+additional", two_line and two_line["amount"] == 45 and two_line["quantity"] == 2, two_line)
    check("historical AP/LATERAL is 45", hist_line and hist_line["amount"] == 45, hist_line)
    check("missing views ignore 5 files", plain_line and plain_line["amount"] == 30 and plain_line["quantity"] == 1, plain_line)
    check("AP SUPINE priced once", combo_line_chest and combo_line_chest["amount"] == 30, combo_line_chest)
    check("second study two views are additional rates", combo_line_spine and combo_line_spine["amount"] == 30, combo_line_spine)
    check("custom tariff 40+7", rated_line and rated_line["amount"] == 47, rated_line)

north_inv = center_invoice_for(north)
south_inv = center_invoice_for(south)
rated_inv = center_invoice_for(rated)
if north_inv:
    totals_consistent(north_inv, "north centre")
    check("north one-view centre charge 30", line_for(north_inv, "FAKE PATIENT ONE")["amount"] == 30)
    check("north two-view centre charge 45", line_for(north_inv, "FAKE PATIENT TWO")["amount"] == 45)
if rated_inv:
    check("rated centre 50+9", line_for(rated_inv, "FAKE PATIENT RATED")["amount"] == 59, rated_inv)

# Other doctor cannot see Ada's work. Centre login cannot open My Work.
bea_work = c.get("/api/reports/my-work", headers=BEA)
check("other doctor my-work empty", bea_work.status_code == 200 and bea_work.json()["total"] == 0, bea_work.text)
ada_work = c.get("/api/reports/my-work", headers=ADA)
check("owner my-work lists fake patients", ada_work.status_code == 200 and ada_work.json()["total"] >= 5, ada_work.text)
check("centre cannot open my-work", c.get("/api/reports/my-work", headers=NORTH).status_code == 403)
denied = c.get(f"/api/reports/{one['id']}", headers=BEA)
check("other doctor denied case", denied.status_code == 403, denied.text)
south_read = c.get(f"/api/invoices/{north_inv['id']}", headers=SOUTH) if north_inv else None
check("other centre denied invoice", south_read is not None and south_read.status_code == 403, getattr(south_read, "text", None))

# Partial case disappears from doctor partial list and my-work after delete.
partial = upload(north, ADA, "FAKE PATIENT PARTIAL", [
    {"bodyPart": "CHEST PA", "views": ["PA"], "images": [], "studyDate": "07-11-2026"},
    {"bodyPart": "KNEE AP", "views": ["AP"], "images": [], "studyDate": "07-11-2026"},
])
first_study = partial["studies"][0]
r = c.post(
    f"/api/reports/{partial['id']}/studies/{first_study['id']}/sign",
    headers=ADA,
    json={"findings": "Partial fake finding."},
)
check("partial sign", r.status_code == 200 and r.json().get("caseComplete") is False, r.text)
partials = c.get("/api/reports/my-partial-cases", headers=BEA)
# Bea did not claim it.
check("other doctor partials empty", partials.status_code == 200 and partials.json()["total"] == 0, partials.text)
partials = c.get("/api/reports/my-partial-cases", headers=ADA)
check("owner sees partial", any(i["id"] == partial["id"] for i in partials.json()["items"]), partials.text)
deleted = c.delete(f"/api/reports/{partial['id']}", headers=SA)
check("delete partial", deleted.status_code == 204, deleted.text)
partials = c.get("/api/reports/my-partial-cases", headers=ADA)
check("deleted partial hidden", all(i["id"] != partial["id"] for i in partials.json()["items"]), partials.text)
ada_work = c.get("/api/reports/my-work", headers=ADA)
check("deleted patient hidden from my-work", all(i["id"] != partial["id"] for i in ada_work.json()["items"]), ada_work.text)
check("delete does not remove other my-work rows", any(i["id"] == one["id"] for i in ada_work.json()["items"]))
check("deleted case 404", c.get(f"/api/reports/{partial['id']}", headers=ADA).status_code == 404)
check("deleted case 404 for other doctor", c.get(f"/api/reports/{partial['id']}", headers=BEA).status_code == 404)

# Delete a billed patient: open invoices drop that row and recompute. Other patients stay.
before_doc = doctor_invoice(ADA)
before_two = line_for(before_doc, "FAKE PATIENT TWO")["amount"]
before_total = before_doc["totalAmount"]
check("delete billed two-view patient", c.delete(f"/api/reports/{two['id']}", headers=SA).status_code == 204)
after_doc = doctor_invoice(ADA)
check("deleted patient row gone", line_for(after_doc, "FAKE PATIENT TWO") is None, after_doc)
check("other patients remain", line_for(after_doc, "FAKE PATIENT ONE") and line_for(after_doc, "FAKE PATIENT HIST"))
check(
    "doctor total reduced by deleted amount only",
    after_doc["totalAmount"] == before_total - before_two,
    (before_total, before_two, after_doc["totalAmount"]),
)
totals_consistent(after_doc, "doctor after delete")
north_after = center_invoice_for(north)
south_after = center_invoice_for(south)
check("north dropped deleted patient", line_for(north_after, "FAKE PATIENT TWO") is None)
check("north kept the other patient", line_for(north_after, "FAKE PATIENT ONE")["amount"] == 30, north_after)
check("south invoice unchanged", line_for(south_after, "FAKE PATIENT HIST")["amount"] == 45, south_after)
check("south plain patient unchanged", line_for(south_after, "FAKE PATIENT PLAIN")["amount"] == 30, south_after)
totals_consistent(north_after, "north after delete")
totals_consistent(south_after, "south after delete")

# Line row is kept, amount unchanged, status marks the exclusion.
db = SessionLocal()
kept = (
    db.query(InvoiceLineItemDB)
    .filter(InvoiceLineItemDB.patient_name == "FAKE PATIENT TWO", InvoiceLineItemDB.amount > 0)
    .all()
)
check("excluded rows still stored", len(kept) >= 2)
check("excluded rows keep amount", all(int(li.amount) in (30, 45) for li in kept), [(li.amount, li.status) for li in kept])
check("excluded status set", all((li.status or "") == "EXCLUDED_DELETED" for li in kept), [li.status for li in kept])
db.close()

# Already-deleted patient repaired on read, without a prior exclude call.
legacy = upload(south, ADA, "FAKE PATIENT LEGACY", [{
    "bodyPart": "CHEST PA",
    "views": ["PA"],
    "images": [],
    "studyDate": "07-11-2026",
}])
sign_all(ADA, legacy)
db = SessionLocal()
row = db.query(CaseDB).filter(CaseDB.id == legacy["id"]).first()
row.deleted_at = "2026-01-01T00:00:00Z"
db.commit()
stored_before = db.query(InvoiceDB).filter(InvoiceDB.party_type == "doctor").one()
stored_total = int(stored_before.total_amount)
preview = preview_open_invoice_deletion_adjustments(db)
db.rollback()
still = int(db.query(InvoiceDB).filter(InvoiceDB.id == stored_before.id).one().total_amount)
db.close()
check("preview does not write", still == stored_total, (still, stored_total))
check(
    "preview names the legacy case",
    any(line["caseId"] == legacy["id"] and item["action"] == "exclude_on_read" for item in preview for line in item["lines"]),
    preview,
)
legacy_doc = doctor_invoice(ADA)
check("legacy deleted row hidden on read", line_for(legacy_doc, "FAKE PATIENT LEGACY") is None)
check("read lowered the stored total", legacy_doc["totalAmount"] < stored_total, (legacy_doc["totalAmount"], stored_total))
again = doctor_invoice(ADA)
check("second read is stable", again["totalAmount"] == legacy_doc["totalAmount"])
check("legacy case hidden from my-work", all(i["id"] != legacy["id"] for i in c.get("/api/reports/my-work", headers=ADA).json()["items"]))

# Paid invoice is not rewritten.
paid_case = upload(north, CARA, "FAKE PATIENT PAID", [{
    "bodyPart": "CHEST PA",
    "views": ["PA"],
    "images": [],
    "studyDate": "07-11-2026",
}])
sign_all(CARA, paid_case)
cara_inv = doctor_invoice(CARA)
check("mark doctor invoice paid", c.patch(
    f"/api/invoices/{cara_inv['id']}/status", headers=SA, json={"status": "paid"}
).status_code == 200)
paid_total = doctor_invoice(CARA)["totalAmount"]
check("delete paid patient", c.delete(f"/api/reports/{paid_case['id']}", headers=SA).status_code == 204)
paid_after = doctor_invoice(CARA)
check("paid total unchanged", paid_after["totalAmount"] == paid_total, (paid_total, paid_after["totalAmount"]))
check("paid detail row kept", line_for(paid_after, "FAKE PATIENT PAID") is not None, paid_after)
check("paid patient still hidden from my-work", all(
    i["id"] != paid_case["id"] for i in c.get("/api/reports/my-work", headers=CARA).json()["items"]
))
db = SessionLocal()
paid_lines = db.query(InvoiceLineItemDB).filter(InvoiceLineItemDB.patient_name == "FAKE PATIENT PAID").all()
check("paid lines not marked excluded", all((li.status or "ACTIVE") != "EXCLUDED_DELETED" for li in paid_lines), [li.status for li in paid_lines])
# View-delta preview must not change the paid total.
before_delta = int(db.query(InvoiceDB).filter(InvoiceDB.id == cara_inv["id"]).one().total_amount)
preview_view_count_deltas(db)
db.rollback()
after_delta = int(db.query(InvoiceDB).filter(InvoiceDB.id == cara_inv["id"]).one().total_amount)
db.close()
check("view delta preview does not write", before_delta == after_delta)

# Reassign keeps the original multi-view amount and still one active payout per study.
re_case = upload(south, ADA, "FAKE PATIENT REASSIGN", [{
    "bodyPart": "KNEE AP/LATERAL",
    "views": ["AP", "LATERAL"],
    "images": [],
    "studyDate": "07-11-2026",
}])
sign_all(ADA, re_case)
docs = c.get("/api/doctors", headers=SA).json()
bea_id = next(d["id"] for d in docs if d.get("email") == "bea@view.test")
r = c.post(f"/api/reports/{re_case['id']}/flag-reassign", headers=SA, json={
    "toDoctorId": bea_id,
    "reason": "Fake reassign check",
})
check("reassign", r.status_code == 200, r.text)
ada_id = next(d["id"] for d in docs if d.get("email") == "ada@view.test")
r = c.put(f"/api/billing/doctor-pricing/{ada_id}", headers=SA, json={"firstView": 80, "additionalView": 40})
check("raise doctor tariff after sign-off", r.status_code == 200 and r.json()["isCustom"] is True, r.text)
r = c.post(
    f"/api/reports/{re_case['id']}/studies/{re_case['studies'][0]['id']}/sign",
    headers=BEA,
    json={"findings": "Re-signed fake finding.", "impression": "Still normal."},
)
check("re-sign", r.status_code == 200, r.text)
bea_inv = doctor_invoice(BEA)
bea_line = line_for(bea_inv, "FAKE PATIENT REASSIGN") if bea_inv else None
check("reissued payout keeps historical 45", bea_line and bea_line["amount"] == 45 and bea_line["quantity"] == 2, bea_line)
south_re = center_invoice_for(south)
re_lines = [li for li in (south_re or {}).get("lineItems") or [] if li["patientName"] == "FAKE PATIENT REASSIGN"]
check("centre not rebilled on reassign", len(re_lines) == 1 and re_lines[0]["amount"] == 45, re_lines)
ada_after_re = doctor_invoice(ADA)
ada_re_lines = [li for li in ada_after_re["lineItems"] if li["patientName"] == "FAKE PATIENT REASSIGN"]
check("original doctor net zero on reassign", sum(int(li["amount"]) for li in ada_re_lines) == 0, ada_re_lines)
check("reassign left one positive and one offset", len(ada_re_lines) == 2, ada_re_lines)

# Orphan line (case id with no case row) drops off an open invoice.
orphan_case = upload(north, ADA, "FAKE PATIENT ORPHAN", [{
    "bodyPart": "CHEST PA",
    "views": ["PA"],
    "images": [],
    "studyDate": "07-11-2026",
}])
sign_all(ADA, orphan_case)
db = SessionLocal()
orphan_lines = db.query(InvoiceLineItemDB).filter(InvoiceLineItemDB.case_id == orphan_case["id"]).all()
for li in orphan_lines:
    li.case_id = "missing-case-orphan"
db.commit()
db.close()
open_doc = doctor_invoice(ADA)
check("orphan row hidden", line_for(open_doc, "FAKE PATIENT ORPHAN") is None, open_doc)
check("combo patient still present", line_for(open_doc, "FAKE PATIENT COMBO") is not None)

# Doctor tariff: custom applies to new sign-offs, zero is allowed, default is 30/15.
fresh = upload(north, ADA, "FAKE PATIENT CUSTOM", [{
    "bodyPart": "CHEST PA",
    "views": ["PA"],
    "images": [],
    "studyDate": "07-11-2026",
}])
add_images(fresh["studies"][0]["id"], 4)
sign_all(ADA, fresh)
custom_inv = doctor_invoice(ADA)
custom_line = line_for(custom_inv, "FAKE PATIENT CUSTOM")
check("custom doctor tariff ignores image count", custom_line and custom_line["amount"] == 80 and custom_line["quantity"] == 1, custom_line)
check("centre charge ignores doctor tariff", line_for(center_invoice_for(north), "FAKE PATIENT CUSTOM")["amount"] == 30)

r = c.put(f"/api/billing/doctor-pricing/{ada_id}", headers=SA, json={"firstView": 0, "additionalView": 0})
check("zero payout allowed", r.status_code == 200 and r.json()["firstView"] == 0 and r.json()["isCustom"] is True, r.text)
zero_case = upload(north, ADA, "FAKE PATIENT ZERO", [{
    "bodyPart": "CHEST PA", "views": ["PA"], "images": [], "studyDate": "07-11-2026",
}])
sign_all(ADA, zero_case)
zero_line = line_for(doctor_invoice(ADA), "FAKE PATIENT ZERO")
check("zero tariff pays zero", zero_line and zero_line["amount"] == 0, zero_line)
check("zero tariff still charges the centre", line_for(center_invoice_for(north), "FAKE PATIENT ZERO")["amount"] == 30)

bea_id_pay = next(d["id"] for d in c.get("/api/doctors", headers=SA).json() if d.get("email") == "bea@view.test")
r = c.put(f"/api/billing/doctor-pricing/{bea_id_pay}", headers=SA, json={"firstView": 18, "additionalView": 9})
check("other doctor historical tariff saved", r.status_code == 200 and r.json()["isCustom"] is True, r.text)

r = c.put(f"/api/billing/doctor-pricing/{ada_id}", headers=SA, json={"clear": True})
check("clear is a separate reset", r.status_code == 200 and r.json()["isCustom"] is False and r.json()["source"] == "unset", r.text)
r = c.put(f"/api/billing/center-pricing/{south}", headers=SA, json={
    "centerFirstStudy": 30, "centerAdditionalStudy": 15, "doctorFirstStudy": 20, "doctorAdditionalStudy": 10,
})
check("centre 20/10 doctor payout left stored", r.status_code == 200, r.text)
centre_case = upload(south, ADA, "FAKE PATIENT CENTRE RATE", [{
    "bodyPart": "CHEST PA", "views": ["PA"], "images": [], "studyDate": "07-11-2026",
}])
sign_all(ADA, centre_case)
check(
    "unset doctor uses stored centre 20",
    line_for(doctor_invoice(ADA), "FAKE PATIENT CENTRE RATE")["amount"] == 20,
)

r = c.put(f"/api/billing/doctor-pricing/{ada_id}", headers=SA, json={"firstView": 30, "additionalView": 15})
check("saving 30/15 persists", r.status_code == 200 and r.json()["isCustom"] is True and r.json()["firstView"] == 30 and r.json()["additionalView"] == 15, r.text)
listed = c.get("/api/billing/doctor-pricing", headers=SA).json()
ada_row = next(d for d in listed["doctors"] if d["doctorId"] == ada_id)
bea_row = next(d for d in listed["doctors"] if d["doctorId"] == bea_id_pay)
check("saved 30/15 still reads 30/15", ada_row["firstView"] == 30 and ada_row["additionalView"] == 15 and ada_row["isCustom"] is True, ada_row)
check("other doctor custom tariff retained", bea_row["firstView"] == 18 and bea_row["additionalView"] == 9, bea_row)
two_override = upload(south, ADA, "FAKE PATIENT OVERRIDE", [{
    "bodyPart": "KNEE AP/LATERAL", "views": ["AP", "LATERAL"], "images": [], "studyDate": "07-11-2026",
}])
sign_all(ADA, two_override)
override_lines = [li for li in doctor_invoice(ADA)["lineItems"] if li["patientName"] == "FAKE PATIENT OVERRIDE"]
check(
    "saved 30/15 overrides centre 20/10 for two views",
    sum(int(li["amount"]) for li in override_lines) == 45,
    override_lines,
)
check("negative rate rejected", c.put(
    f"/api/billing/doctor-pricing/{ada_id}", headers=SA, json={"firstView": -1, "additionalView": 15}
).status_code == 422)
r = c.post("/api/managers", headers=SA, json={
    "name": "Pay Mgr", "email": "paymgr@view.test", "password": "PayMgr123!",
    "permissions": {"doctors": {"read": True, "write": True}},
})
check("create payout manager", r.status_code == 200, r.text)
MGR, _ = login("paymgr@view.test", "PayMgr123!")
check("manager cannot set doctor payout", c.put(
    f"/api/billing/doctor-pricing/{ada_id}", headers=MGR, json={"firstView": 1, "additionalView": 1}
).status_code == 403)
check("doctor cannot set doctor payout", c.put(
    f"/api/billing/doctor-pricing/{ada_id}", headers=ADA, json={"firstView": 1, "additionalView": 1}
).status_code == 403)
check("doctor cannot list payout admin", c.get("/api/billing/doctor-pricing", headers=ADA).status_code == 403)

failed = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
if failed:
    sys.exit(1)
