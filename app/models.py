from sqlalchemy import Column, String, Integer, Text, JSON, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
try:
    from app.database import Base
except ImportError:
    from app.database import Base

# Universal JSON field that works with PostgreSQL (JSONB) and SQLite (JSON)
JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")

class UserDB(Base):
    __tablename__ = "users"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False)  # SUPER_ADMIN, DOCTOR, CENTER, MANAGER
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class DoctorDB(Base):
    __tablename__ = "doctors"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    full_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    username = Column(String(100), nullable=True)
    contact_number = Column(String(50), nullable=False)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class CenterDB(Base):
    __tablename__ = "radiology_centers"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    center_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    contact_number = Column(String(50), nullable=False)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class CaseDB(Base):
    __tablename__ = "cases"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    patient_number = Column(String(100), nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=False)
    gender = Column(String(50), nullable=False)
    phone = Column(String(50), nullable=False)
    radiology_center_id = Column(String(100), nullable=False, index=True)
    radiology_center_name = Column(String(255), nullable=False)
    referring_physician_id = Column(String(100), nullable=False)
    referring_physician_name = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False, default="Pending", index=True)
    is_urgent = Column(Boolean, default=False, nullable=True, index=True)
    study_date = Column(String(50), nullable=False)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class StudyDB(Base):
    __tablename__ = "studies"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    case_id = Column(String(100), ForeignKey("cases.id"), nullable=False, index=True)
    modality = Column(String(100), nullable=False, default="X-Ray")
    body_part = Column(String(255), nullable=False)
    status = Column(String(50), nullable=False, default="UNCLAIMED", index=True) # UNCLAIMED, CLAIMED, Completed
    is_urgent = Column(Boolean, default=False, nullable=True, index=True)
    clinical_notes = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)
    impression = Column(Text, nullable=True)
    doc_content = Column(Text, nullable=True)
    claimed_by = Column(String(100), nullable=True, index=True)
    claimed_by_name = Column(String(255), nullable=True)
    # Per-study report fields (one report per study)
    report_status = Column(String(20), nullable=False, default="PENDING", index=True)  # PENDING | DRAFT | SIGNED
    technique = Column(Text, nullable=True)
    template_id = Column(String(100), nullable=True)
    signed_at = Column(String(50), nullable=True)
    signed_by = Column(String(100), nullable=True)
    signed_by_name = Column(String(255), nullable=True)
    # 1-based position of the study in the case's original bodyParts upload order
    # (billing study_index). NULL on legacy rows (never backfilled).
    sequence_no = Column(Integer, nullable=True)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class StudyImageDB(Base):
    __tablename__ = "study_images"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    study_id = Column(String(100), ForeignKey("studies.id"), nullable=False, index=True)
    image_url = Column(Text, nullable=False)
    image_type = Column(String(50), nullable=False, default="uploaded")
    created_at = Column(String(50), nullable=False)

class StudyNotificationDB(Base):
    __tablename__ = "study_notifications"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    study_id = Column(String(100), ForeignKey("studies.id"), nullable=False, index=True)
    doctor_id = Column(String(100), nullable=False, index=True)
    notified_at = Column(String(50), nullable=False)
    dismissed_reason = Column(String(50), nullable=True) # accepted_self, accepted_other, null

class ReportDB(Base):
    __tablename__ = "xray_reports"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    patient_number = Column(String(100), nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=False)
    gender = Column(String(50), nullable=False)
    phone = Column(String(50), nullable=False)
    radiology_center_id = Column(String(100), nullable=False)
    radiology_center_name = Column(String(255), nullable=False)
    referring_physician_id = Column(String(100), nullable=False)
    referring_physician_name = Column(String(255), nullable=False)
    assigned_doctor_id = Column(String(100), nullable=True)
    assigned_doctor_name = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="Pending", index=True)
    is_urgent = Column(Boolean, default=False, nullable=True, index=True)
    study_date = Column(String(50), nullable=False)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class TemplateDB(Base):
    __tablename__ = "doc_templates"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    title = Column(String(255), nullable=False)
    center_id = Column(String(100), nullable=False, default="ALL")
    center_name = Column(String(255), nullable=False, default="All Centers")
    modality = Column(String(100), nullable=False)
    created_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})

class PendingApprovalDB(Base):
    __tablename__ = "pending_approvals"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    manager_id = Column(String(100), nullable=False, index=True)
    manager_name = Column(String(255), nullable=False)
    action_type = Column(String(100), nullable=False, index=True) # CREATE_CASE, UPDATE_CASE, DELETE_CASE, etc.
    entity_type = Column(String(100), nullable=False, index=True) # case, doctor, center, template
    entity_id = Column(String(100), nullable=True, index=True)
    payload = Column("payload", JSON_TYPE, nullable=True, default={})
    status = Column(String(50), nullable=False, default="PENDING", index=True) # PENDING, APPROVED, REJECTED
    rejection_reason = Column(Text, nullable=True)
    created_at = Column(String(50), nullable=False)
    reviewed_at = Column(String(50), nullable=True)
    reviewed_by = Column(String(255), nullable=True)


class BillingPeriodLockDB(Base):
    """Super Admin lock for a billing calendar month (YYYY-MM, Asia/Calcutta)."""
    __tablename__ = "billing_period_locks"
    __table_args__ = {'extend_existing': True}

    period = Column(String(7), primary_key=True)  # YYYY-MM
    locked = Column(Boolean, default=False, nullable=False)
    locked_at = Column(String(50), nullable=True)
    locked_by = Column(String(255), nullable=True)


class CenterPricingDB(Base):
    """Per-centre rate card (INR) used for NEW sign-off billing only.
    No row for a centre = default rates (pricing.DEFAULT_RATES). Line items keep
    the amount they were billed at, so editing a row never changes past bills."""
    __tablename__ = "center_pricing"
    __table_args__ = {'extend_existing': True}

    center_id = Column(String(100), primary_key=True)
    center_first_inr = Column(Integer, nullable=False)
    center_additional_inr = Column(Integer, nullable=False)
    doctor_first_inr = Column(Integer, nullable=False)
    doctor_additional_inr = Column(Integer, nullable=False)
    updated_at = Column(String(50), nullable=True)
    updated_by = Column(String(255), nullable=True)


class InvoiceDB(Base):
    """Monthly center charge or doctor payout invoice (immutable totals when locked)."""
    __tablename__ = "invoices"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    party_type = Column(String(20), nullable=False, index=True)  # center | doctor
    party_id = Column(String(100), nullable=False, index=True)
    party_name = Column(String(255), nullable=False)
    billing_period = Column(String(7), nullable=False, index=True)  # YYYY-MM IST
    status = Column(String(20), nullable=False, default="pending", index=True)  # paid | pending (overdue computed)
    locked = Column(Boolean, default=False, nullable=False, index=True)
    total_amount = Column(Integer, nullable=False, default=0)  # INR whole rupees
    currency = Column(String(10), nullable=False, default="INR")
    linked_invoice_id = Column(String(100), nullable=True, index=True)
    created_at = Column(String(50), nullable=False)
    updated_at = Column(String(50), nullable=False)
    metadata_ = Column("metadata", JSON_TYPE, nullable=True, default={})


class InvoiceLineItemDB(Base):
    """Immutable snapshot line for one study charge/payout accrued at sign-off."""
    __tablename__ = "invoice_line_items"
    __table_args__ = {'extend_existing': True}

    id = Column(String(100), primary_key=True, index=True)
    invoice_id = Column(String(100), ForeignKey("invoices.id"), nullable=False, index=True)
    paired_line_item_id = Column(String(100), nullable=True, index=True)
    billing_event_id = Column(String(100), nullable=False, index=True)
    case_id = Column(String(100), nullable=False, index=True)
    study_id = Column(String(100), nullable=False, index=True)
    study_index = Column(Integer, nullable=False, default=1)
    service_date = Column(String(50), nullable=False)
    description = Column(String(500), nullable=False)
    modality = Column(String(100), nullable=True)
    body_part = Column(String(255), nullable=True)
    patient_number = Column(String(100), nullable=True)
    patient_name = Column(String(255), nullable=True)
    center_id = Column(String(100), nullable=True, index=True)
    center_name = Column(String(255), nullable=True)
    doctor_id = Column(String(100), nullable=True, index=True)
    doctor_name = Column(String(255), nullable=True)
    unit_amount = Column(Integer, nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    amount = Column(Integer, nullable=False)
    currency = Column(String(10), nullable=False, default="INR")
    created_at = Column(String(50), nullable=False)
    # P6 reassign fix (additive, nullable): payout clawback tracking.
    # status: ACTIVE (NULL treated as ACTIVE) | CLAWED_BACK (original, reversed) | CLAWBACK (negative offset line)
    status = Column(String(20), nullable=True, default="ACTIVE")
    status_reason = Column(Text, nullable=True)
    offsets_line_item_id = Column(String(100), nullable=True, index=True)


class ReportPublicLinkDB(Base):
    """Unguessable token for the public, read-only view of one signed study report.
    Created when the study is first signed; revoked_at set = link no longer works."""
    __tablename__ = "report_public_links"
    __table_args__ = {'extend_existing': True}

    token = Column(String(64), primary_key=True)
    case_id = Column(String(100), nullable=False, index=True)
    study_id = Column(String(100), nullable=False, index=True)
    created_at = Column(String(50), nullable=False)
    revoked_at = Column(String(50), nullable=True)


class ReportCommentDB(Base):
    """Chronological flag / reassign / recheck / comment thread per case."""
    __tablename__ = "report_comments"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    case_id = Column(String(100), nullable=False, index=True)
    study_id = Column(String(100), nullable=True, index=True)
    author_user_id = Column(Integer, nullable=True, index=True)
    author_role = Column(String(50), nullable=False)
    author_name = Column(String(255), nullable=False)
    kind = Column(String(20), nullable=False, index=True)  # FLAG | REASSIGN | RECHECK | COMMENT
    body = Column(Text, nullable=False)
    from_doctor_id = Column(String(100), nullable=True)
    to_doctor_id = Column(String(100), nullable=True)
    created_at = Column(String(50), nullable=False)


class UserNotificationDB(Base):
    """Per-user bell notification (reassigned case, new case-activity message)."""
    __tablename__ = "user_notifications"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    kind = Column(String(40), nullable=False)  # CASE_REASSIGNED | CASE_MESSAGE
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    case_id = Column(String(100), nullable=True, index=True)
    link = Column(String(255), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    actor_name = Column(String(255), nullable=True)
    created_at = Column(String(50), nullable=False)
    read_at = Column(String(50), nullable=True)



class UserCenterAccessDB(Base):
    """Which radiology centres a centre-side login (role CENTER) can work with, and how.

    One row per (user, centre). is_admin = Center Admin for that centre (may manage
    the centre's users). permissions = {"reports"|"invoices"|"templates"|"center_info":
    "none"|"read"|"write"}. A CENTER account with no rows falls back to its
    metadata.centerId as admin with full rights (pre multi-centre accounts)."""
    __tablename__ = "user_center_access"
    __table_args__ = (
        UniqueConstraint("user_id", "center_id", name="uq_user_center_access_user_center"),
        {'extend_existing': True},
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    center_id = Column(String(100), nullable=False, index=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    permissions = Column(JSON_TYPE, nullable=True, default={})
    created_at = Column(String(50), nullable=False)
    created_by = Column(String(255), nullable=True)


class ImageAnnotationDB(Base):
    """Persistent mark drawn on a case image in the PACS viewer (e.g. an arrow).

    image_key identifies the image independent of the public domain: the stored
    file path (what follows /api/files/) or "img-<index>" for unstored images.
    data holds normalised 0..1000 image coordinates, e.g. {"x1","y1","x2","y2"}."""
    __tablename__ = "image_annotations"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    case_id = Column(String(100), nullable=False, index=True)
    image_key = Column(String(500), nullable=False)
    kind = Column(String(20), nullable=False, default="arrow")
    data = Column(JSON_TYPE, nullable=False)
    author_user_id = Column(Integer, nullable=True)
    author_name = Column(String(255), nullable=True)
    author_role = Column(String(50), nullable=True)
    created_at = Column(String(50), nullable=False)
