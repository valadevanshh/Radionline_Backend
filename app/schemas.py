from pydantic import BaseModel
from typing import List, Optional, Dict, Any

# User Account Schemas
class UserBase(BaseModel):
    email: str
    name: str
    role: str
    doctorId: Optional[str] = None
    centerId: Optional[str] = None
    avatar: Optional[str] = None

class UserCreate(UserBase):
    password: Optional[str] = None

class UserResponse(UserBase):
    class Config:
        from_attributes = True

class LoginRequest(BaseModel):
    email: str
    password: str

class SessionUserResponse(UserResponse):
    """Login payload: the user plus what the UI needs for centre scoping."""
    userId: Optional[int] = None
    centers: Optional[List[Dict[str, Any]]] = None
    centerCount: Optional[int] = None
    multiCenter: Optional[bool] = None
    pagePermissions: Optional[Dict[str, str]] = None
    canManageCenterUsers: Optional[bool] = None

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: SessionUserResponse

# Doctor Schemas
class DoctorBase(BaseModel):
    firstName: str
    lastName: str
    fullName: str
    email: str
    username: Optional[str] = None
    password: Optional[str] = None
    contactNumber: str
    address: Optional[str] = None
    signatureUrl: Optional[str] = None
    profileFileUrl: Optional[str] = None
    degree: Optional[str] = "M.D. (Radiodiagnosis)"
    registrationNumber: Optional[str] = None

class DoctorCreate(DoctorBase):
    id: Optional[str] = None

class DoctorResponse(DoctorBase):
    id: str
    createdAt: str

    class Config:
        from_attributes = True

# Radiology Center Schemas
class CenterBase(BaseModel):
    centerName: str
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    email: str
    username: Optional[str] = None
    password: Optional[str] = None
    contactNumber: str
    address: Optional[str] = None
    headerTemplateUrl: Optional[str] = None
    letterheadMode: Optional[str] = None
    logoUrl: Optional[str] = None

class CenterCreate(CenterBase):
    id: Optional[str] = None

class CenterResponse(CenterBase):
    id: str
    createdAt: str

    class Config:
        from_attributes = True

# XRay Report Schemas
class ReportBase(BaseModel):
    patientNumber: str
    fullName: str
    age: int
    ageUnit: Optional[str] = "Years"
    gender: str
    phone: str
    radiologyCenterId: str
    radiologyCenterName: str
    bodyParts: List[str]
    referringPhysicianId: str
    referringPhysicianName: str
    assignedDoctorId: Optional[str] = None
    assignedDoctorName: Optional[str] = None
    assignedDoctorDegree: Optional[str] = "M.D. (Radiodiagnosis)"
    assignedDoctorRegNo: Optional[str] = None
    assignedDoctorIds: Optional[List[str]] = None
    claimedByDoctorId: Optional[str] = None
    claimedByDoctorName: Optional[str] = None
    claimStatus: Optional[str] = "UNCLAIMED"
    status: str = "Pending"
    studyDate: str
    clinicalNotes: Optional[str] = None
    findings: Optional[str] = None
    impression: Optional[str] = None
    reportsByBodyPart: Optional[Dict[str, str]] = None
    impressionsByBodyPart: Optional[Dict[str, str]] = None
    dicomFileUrl: Optional[str] = None
    dicomMetadata: Optional[Dict[str, str]] = None
    docContent: Optional[str] = None
    dicomSnapshots: Optional[List[str]] = None
    uploadedImages: Optional[List[str]] = None
    clinicalHistoryImages: Optional[List[str]] = None
    hasHeaderUrl: Optional[bool] = True
    hasNoHeaderUrl: Optional[bool] = True
    signatureApplied: Optional[bool] = False
    isUrgent: Optional[bool] = False
    isPortable: Optional[bool] = False
    # Study modality: X-Ray | CT | MRI | Sonography | Blood Report
    modality: Optional[str] = None

class ReportCreate(ReportBase):
    id: Optional[str] = None

class ReportResponse(ReportBase):
    id: str
    createdAt: str

    class Config:
        from_attributes = True

class RejectReportRequest(BaseModel):
    doctorId: str
    doctorName: Optional[str] = None
    reason: Optional[str] = "rejected"


# Template Schemas
class TemplateBase(BaseModel):
    title: str
    centerId: str = "ALL"
    centerName: str = "All Centers"
    modality: str
    bodyPart: Optional[str] = None
    findings: Optional[str] = None
    impression: Optional[str] = None
    content: Optional[str] = None

class TemplateCreate(TemplateBase):
    id: Optional[str] = None

class TemplateResponse(TemplateBase):
    id: str
    createdAt: str

    class Config:
        from_attributes = True


# Approval Schemas for Manager Staging & Super Admin Approvals
class ApprovalSubmitRequest(BaseModel):
    managerId: str
    managerName: str
    actionType: str # CREATE_CASE, UPDATE_CASE, DELETE_CASE, CREATE_DOCTOR, UPDATE_DOCTOR, DELETE_DOCTOR, CREATE_CENTER, UPDATE_CENTER, DELETE_CENTER, CREATE_TEMPLATE, UPDATE_TEMPLATE, DELETE_TEMPLATE
    entityType: str # case, doctor, center, template
    entityId: Optional[str] = None
    payload: Dict[str, Any]

class ApprovalReviewRequest(BaseModel):
    reviewerName: Optional[str] = "Super Admin"
    rejectionReason: Optional[str] = None

class ApprovalResponse(BaseModel):
    id: str
    managerId: str
    managerName: str
    actionType: str
    entityType: str
    entityId: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    before: Optional[Dict[str, Any]] = None  # Priority 7: current record for before/after diffs
    status: str # PENDING, APPROVED, REJECTED
    rejectionReason: Optional[str] = None
    createdAt: str
    reviewedAt: Optional[str] = None
    reviewedBy: Optional[str] = None

    class Config:
        from_attributes = True


# Case activity thread (Priority 6): flags, reassign, recheck, comments
class ReportCommentCreate(BaseModel):
    body: str


class FlagReassignRequest(BaseModel):
    reason: str
    toDoctorId: str


class RecheckRequest(BaseModel):
    reason: str


class ReportCommentResponse(BaseModel):
    id: int
    caseId: str
    studyId: Optional[str] = None
    authorUserId: Optional[int] = None
    authorRole: str
    authorName: str
    kind: str  # FLAG | REASSIGN | RECHECK | COMMENT
    body: str
    fromDoctorId: Optional[str] = None
    toDoctorId: Optional[str] = None
    createdAt: str

    class Config:
        from_attributes = True

