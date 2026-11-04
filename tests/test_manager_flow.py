"""Isolated test: Manager accounts + per-page ACL + SA approval. SQLite only, never Postgres."""
import os, sys, tempfile, shutil
TMP = tempfile.mkdtemp(prefix="mgrtest_")
os.environ["DATABASE_URL"] = "postgresql://nobody:nothing@127.0.0.1:1/none"  # unreachable -> sqlite
os.environ["ALLOW_SQLITE_FALLBACK"] = "true"
os.environ["ENVIRONMENT"] = "development"
os.environ["FILE_STORAGE_TRANSPORT"] = "local"
os.environ["FILE_STORAGE_ROOT"] = os.path.join(TMP, "files")
os.environ["JWT_SECRET"] = "test-secret"
BACKEND = r"C:\Project\Radionline\Radionline_Backend"
sys.path.insert(0, BACKEND)
os.chdir(TMP)  # ./radionline.db lands in the temp dir

from app.database import engine, Base
assert engine.dialect.name == "sqlite", engine.url
assert TMP.replace("\\", "/").lower() in str(engine.url).replace("\\", "/").lower() or "./radionline.db" in str(engine.url), engine.url
print("DB:", engine.url, "(cwd", TMP, ")")
import app.models  # noqa
Base.metadata.create_all(bind=engine)
from app.main import app
from fastapi.testclient import TestClient
from app.database import SessionLocal
from app.models import UserDB, DoctorDB, CenterDB, PendingApprovalDB
from app.security import hash_password

c = TestClient(app)
results = []
def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (("  -> " + str(extra)[:300]) if not cond else ""))

db = SessionLocal()
for u in db.query(UserDB).all():
    pass
sa = db.query(UserDB).filter(UserDB.email == "sa@test.local").first()
if not sa:
    db.add(UserDB(email="sa@test.local", name="SA", role="SUPER_ADMIN", metadata_={"password": hash_password("SaPass123!")}))
db.add(UserDB(email="legacy.mgr@test.local", name="Legacy Mgr", role="MANAGER", metadata_={"password": hash_password("Legacy123!")}))
db.commit(); db.close()

def login(e, p):
    r = c.post("/api/auth/login", json={"email": e, "password": p})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}, r.json()["user"]
SA, _ = login("sa@test.local", "SaPass123!")

# --- SA creates centre + doctor directly (immediate)
r = c.post("/api/centers", headers=SA, json={"centerName": "Alpha Scan", "email": "alpha@test.local", "contactNumber": "9999999999", "username": "alpha@test.local", "password": "Alpha123!"})
check("SA create centre applies immediately (200)", r.status_code == 200, r.text)
center_id = r.json().get("id")
r = c.post("/api/doctors", headers=SA, json={"firstName": "Ann", "lastName": "Ray", "fullName": "Ann Ray", "email": "ann@test.local", "contactNumber": "8888888888", "username": "ann@test.local", "password": "AnnRay123!"})
check("SA create doctor applies immediately (200)", r.status_code == 200, r.text)
doctor_id = r.json().get("id")

# --- 1/2: SA creates Managers with per-page rights
perms_a = {"reports": {"read": True}, "doctors": {"read": True, "write": True}, "centers": {"write": True}}
r = c.post("/api/managers", headers=SA, json={"name": "Mgr A", "email": "mgra@test.local", "password": "MgrA1234!", "permissions": perms_a})
check("SA creates manager A", r.status_code == 200, r.text)
mgr_a = r.json()
check("write implies read (centers)", mgr_a["permissions"]["centers"] == {"read": True, "write": True, "delete": False}, mgr_a)
check("unset delete stays false", mgr_a["permissions"]["doctors"]["delete"] is False)
r = c.post("/api/managers", headers=SA, json={"name": "Mgr B", "email": "mgrb@test.local", "password": "MgrB1234!", "permissions": {}})
check("SA creates manager B with no pages", r.status_code == 200, r.text)
mgr_b = r.json()
r = c.post("/api/managers", headers=SA, json={"name": "Dup", "email": "MGRA@test.local", "password": "MgrA1234!", "permissions": {}})
check("duplicate email rejected 409", r.status_code == 409, r.text)
r = c.post("/api/managers", headers=SA, json={"name": "Short", "email": "s@test.local", "password": "x", "permissions": {}})
check("short password rejected 400", r.status_code == 400, r.text)
r = c.get("/api/managers", headers=SA)
check("SA lists managers (A, B, legacy)", r.status_code == 200 and {m["email"] for m in r.json()} >= {"mgra@test.local", "mgrb@test.local", "legacy.mgr@test.local"}, r.text)

A, a_user = login("mgra@test.local", "MgrA1234!")
B, b_user = login("mgrb@test.local", "MgrB1234!")
check("login payload carries managerPermissions", a_user.get("managerPermissions", {}).get("doctors", {}).get("write") is True, a_user)
me = c.get("/api/auth/me", headers=B).json()
check("/auth/me for manager B shows no access", all(not any(v.values()) for v in me["managerPermissions"].values()), me)

# Manager cannot manage managers / logins / referrers / sign
check("manager cannot list managers", c.get("/api/managers", headers=A).status_code == 403)
check("manager cannot create managers", c.post("/api/managers", headers=A, json={"name": "x", "email": "x@test.local", "password": "Xxxxxxxx1", "permissions": {}}).status_code == 403)
check("manager cannot POST /auth/users", c.post("/api/auth/users", headers=A, json={"email": "z@test.local", "name": "z", "role": "DOCTOR", "password": "Zzzzzzzz1"}).status_code == 403)
check("manager cannot add referrer directly", c.post(f"/api/centers/{center_id}/referrers", headers=A, json={"name": "Dr X"}).status_code == 403)

# --- 3: reads apply per grant
check("A (reports read) GET /reports 200", c.get("/api/reports", headers=A).status_code == 200)
check("B (no reports) GET /reports 403", c.get("/api/reports", headers=B).status_code == 403)
check("A (doctors read) directory 200", c.get("/api/doctors/directory", headers=A).status_code == 200)
check("B (no doctors) directory 403", c.get("/api/doctors/directory", headers=B).status_code == 403)
check("A sees own submissions page (approvals GET)", c.get("/api/approvals", headers=A).status_code == 200)

# --- 4: writes go to approval
r = c.post("/api/doctors", headers=A, json={"firstName": "Bob", "lastName": "Lee", "fullName": "Bob Lee", "email": "bob@test.local", "contactNumber": "7777777777"})
check("A create doctor -> 202 pending", r.status_code == 202 and r.json().get("pendingApproval"), r.text)
appr_doc = r.json()["approval"]["id"]
db = SessionLocal()
check("pending doctor NOT live yet", db.query(DoctorDB).filter(DoctorDB.email == "bob@test.local").first() is None)
db.close()
check("A cannot approve own request", c.post(f"/api/approvals/{appr_doc}/approve", headers=A).status_code == 403)
r = c.post(f"/api/approvals/{appr_doc}/approve", headers=SA)
check("SA approves doctor create", r.status_code == 200 and r.json()["status"] == "APPROVED", r.text)
db = SessionLocal()
check("approved doctor is live", db.query(DoctorDB).filter(DoctorDB.email == "bob@test.local").first() is not None)
db.close()

check("A (doctors no delete) DELETE doctor 403", c.delete(f"/api/doctors/{doctor_id}", headers=A).status_code == 403)
check("B (no doctors write) create doctor 403", c.post("/api/doctors", headers=B, json={"firstName": "C", "lastName": "D", "fullName": "C D", "email": "cd@test.local", "contactNumber": "1"}).status_code == 403)
check("B freeze doctor 403", c.put(f"/api/doctors/{doctor_id}/freeze", headers=B, json={"frozen": True}).status_code == 403)
check("A (reports read only) DELETE report 403", c.delete("/api/reports/any-id", headers=A).status_code == 403)
check("A (reports read only) PATCH report 403", c.patch("/api/reports/any-id", headers=A, json={"fullName": "X"}).status_code == 403)

r = c.post(f"/api/centers/{center_id}/referrers", headers=SA, json={"name": "Dr Ref"})
check("SA adds referrer", r.status_code == 200, r.text)
# --- Centre edit (t134u): Manager with centres write -> approval; SA -> immediate
r = c.post("/api/centers", headers=A, json={"id": center_id, "centerName": "Alpha Scan Renamed", "email": "alpha@test.local", "contactNumber": "9999999999"})
check("A edit centre -> 202 pending", r.status_code == 202, r.text)
appr_c = r.json()["approval"]["id"]
check("pending row is UPDATE_CENTER", r.json()["approval"]["actionType"] == "UPDATE_CENTER", r.json())
db = SessionLocal()
check("centre name unchanged before approval", db.query(CenterDB).get(center_id).center_name == "Alpha Scan")
db.close()
lst = c.get("/api/approvals?status=PENDING", headers=SA).json()
row = next((x for x in lst if x["id"] == appr_c), None)
check("SA sees before/after diff for centre edit", row and row["before"] and row["before"].get("centerName") == "Alpha Scan", row)
r = c.post(f"/api/approvals/{appr_c}/approve", headers=SA)
check("SA approves centre edit", r.status_code == 200, r.text)
db = SessionLocal()
check("centre edit live after approval", db.query(CenterDB).get(center_id).center_name == "Alpha Scan Renamed")
db.close()
r = c.post("/api/centers", headers=SA, json={"id": center_id, "centerName": "Alpha By SA", "email": "alpha@test.local", "contactNumber": "9999999999"})
check("SA edit centre immediate 200", r.status_code == 200, r.text)
db = SessionLocal()
check("SA centre edit live at once", db.query(CenterDB).get(center_id).center_name == "Alpha By SA")
db.close()
check("centre login password survives edits", c.post("/api/auth/login", json={"email": "alpha@test.local", "password": "Alpha123!"}).status_code == 200)
check("saved referrers survive edits", any(x["name"] == "DR REF" for x in c.get(f"/api/centers/{center_id}/referrers", headers=SA).json()))
check("A (centres no delete) DELETE centre 403", c.delete(f"/api/centers/{center_id}", headers=A).status_code == 403)
check("B (no centres write) edit centre 403", c.post("/api/centers", headers=B, json={"id": center_id, "centerName": "B", "email": "a@b.c", "contactNumber": "1"}).status_code == 403)

# --- 5: reject
r = c.put(f"/api/centers/{center_id}/freeze", headers=A, json={"frozen": True})
check("A freeze centre -> 202", r.status_code == 202, r.text)
appr_f = r.json()["approval"]["id"]
r = c.post(f"/api/approvals/{appr_f}/reject", headers=SA, json={"reason": "Not now"})
check("SA rejects freeze", r.status_code == 200 and r.json()["status"] == "REJECTED", r.text)
db = SessionLocal()
check("rejected freeze not applied", not db.query(CenterDB).get(center_id).frozen_at)
db.close()
mine = c.get("/api/approvals", headers=A).json()
check("A sees own decisions incl. rejection reason", any(x["id"] == appr_f and x["status"] == "REJECTED" and x["rejectionReason"] for x in mine), mine)
check("B sees none of A's submissions", c.get("/api/approvals", headers=B).json() == [])

# --- SA changes permissions; take effect immediately (same token)
r = c.put(f"/api/managers/{mgr_b['id']}", headers=SA, json={"permissions": {"doctors": {"read": True, "delete": True}}})
check("SA updates B permissions", r.status_code == 200 and r.json()["permissions"]["doctors"]["delete"], r.text)
check("B now reads doctors directory (no re-login)", c.get("/api/doctors/directory", headers=B).status_code == 200)
r = c.delete(f"/api/doctors/{doctor_id}", headers=B)
check("B delete doctor -> 202 pending", r.status_code == 202, r.text)
db = SessionLocal()
d = db.query(DoctorDB).get(doctor_id)
check("doctor still live before approval", d is not None and not getattr(d, "deleted_at", None))
db.close()

# legacy manager (no permissions key) keeps full access
L, l_user = login("legacy.mgr@test.local", "Legacy123!")
check("legacy manager full rights", all(all(v.values()) for v in l_user["managerPermissions"].values()), l_user)
check("legacy manager reads reports", c.get("/api/reports", headers=L).status_code == 200)

# password reset by SA ends old sessions; deactivate blocks login
import time; time.sleep(1.1)
r = c.put(f"/api/managers/{mgr_a['id']}", headers=SA, json={"password": "NewPass123!"})
check("SA resets manager password", r.status_code == 200)
check("old manager token rejected", c.get("/api/auth/me", headers=A).status_code == 401)
r = c.delete(f"/api/managers/{mgr_a['id']}", headers=SA)
check("SA deactivates manager", r.status_code == 204, r.text)
check("deactivated manager cannot log in", c.post("/api/auth/login", json={"email": "mgra@test.local", "password": "NewPass123!"}).status_code == 401)
check("deactivated manager hidden from list", all(m["email"] != "mgra@test.local" for m in c.get("/api/managers", headers=SA).json()))

# non-manager roles unaffected
check("SA reads reports", c.get("/api/reports", headers=SA).status_code == 200)
check("SA reads directory", c.get("/api/doctors/directory", headers=SA).status_code == 200)

fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} passed")
engine.dispose()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
