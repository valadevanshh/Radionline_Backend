-- ============================================================================
-- RadioNet PACS Platform — Master Database Migration Script
-- Database Engine: PostgreSQL
-- File Location: backend/migrations/scripts.sql
-- ============================================================================

-- 1. USERS TABLE (System Login Accounts & Roles)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL, -- SUPER_ADMIN, DOCTOR, CENTER, MANAGER
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- 2. DOCTORS TABLE (Radiologists Directory)
CREATE TABLE IF NOT EXISTS doctors (
    id VARCHAR(100) PRIMARY KEY,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    username VARCHAR(100),
    contact_number VARCHAR(50) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_doctors_email ON doctors(email);

-- 3. RADIOLOGY CENTERS TABLE (Diagnostic Centers Directory)
CREATE TABLE IF NOT EXISTS radiology_centers (
    id VARCHAR(100) PRIMARY KEY,
    center_name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    contact_number VARCHAR(50) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- 4. CASES TABLE (Patient Demographics & Case Info)
CREATE TABLE IF NOT EXISTS cases (
    id VARCHAR(100) PRIMARY KEY,
    patient_number VARCHAR(100) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    age INTEGER NOT NULL,
    gender VARCHAR(50) NOT NULL,
    phone VARCHAR(50) NOT NULL,
    radiology_center_id VARCHAR(100) NOT NULL,
    radiology_center_name VARCHAR(255) NOT NULL,
    referring_physician_id VARCHAR(100) NOT NULL,
    referring_physician_name VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'Pending',
    study_date VARCHAR(50) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_cases_patient_number ON cases(patient_number);
CREATE INDEX IF NOT EXISTS idx_cases_center_id ON cases(radiology_center_id);

-- 5. STUDIES TABLE (Individual Billable Studies per Body Part)
CREATE TABLE IF NOT EXISTS studies (
    id VARCHAR(100) PRIMARY KEY,
    case_id VARCHAR(100) NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    modality VARCHAR(100) NOT NULL DEFAULT 'X-Ray',
    body_part VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'UNCLAIMED', -- UNCLAIMED, CLAIMED, Completed
    clinical_notes TEXT,
    findings TEXT,
    impression TEXT,
    doc_content TEXT,
    claimed_by VARCHAR(100),
    claimed_by_name VARCHAR(255),
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_studies_case_id ON studies(case_id);
CREATE INDEX IF NOT EXISTS idx_studies_status ON studies(status);
CREATE INDEX IF NOT EXISTS idx_studies_claimed_by ON studies(claimed_by);

-- 6. STUDY IMAGES TABLE (Linked Images per Study)
CREATE TABLE IF NOT EXISTS study_images (
    id VARCHAR(100) PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    image_url TEXT NOT NULL,
    image_type VARCHAR(50) NOT NULL DEFAULT 'uploaded', -- uploaded, snapshot
    created_at VARCHAR(50) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_study_images_study_id ON study_images(study_id);

-- 7. STUDY NOTIFICATIONS TABLE (Doctor Notifications & Single Source of Truth for Queue Dismissal)
CREATE TABLE IF NOT EXISTS study_notifications (
    id SERIAL PRIMARY KEY,
    study_id VARCHAR(100) NOT NULL REFERENCES studies(id) ON DELETE CASCADE,
    doctor_id VARCHAR(100) NOT NULL,
    notified_at VARCHAR(50) NOT NULL,
    dismissed_reason VARCHAR(50) -- accepted_self, accepted_other, null
);

CREATE INDEX IF NOT EXISTS idx_study_notif_study ON study_notifications(study_id);
CREATE INDEX IF NOT EXISTS idx_study_notif_doctor ON study_notifications(doctor_id);

-- 8. LEGACY XRAY REPORTS TABLE (Backward Compatibility)
CREATE TABLE IF NOT EXISTS xray_reports (
    id VARCHAR(100) PRIMARY KEY,
    patient_number VARCHAR(100) NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    age INTEGER NOT NULL,
    gender VARCHAR(50) NOT NULL,
    phone VARCHAR(50) NOT NULL,
    radiology_center_id VARCHAR(100) NOT NULL,
    radiology_center_name VARCHAR(255) NOT NULL,
    referring_physician_id VARCHAR(100) NOT NULL,
    referring_physician_name VARCHAR(255) NOT NULL,
    assigned_doctor_id VARCHAR(100),
    assigned_doctor_name VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'Pending',
    study_date VARCHAR(50) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- 9. DOC TEMPLATES TABLE (Master Reporting Templates)
CREATE TABLE IF NOT EXISTS doc_templates (
    id VARCHAR(100) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    center_id VARCHAR(100) NOT NULL DEFAULT 'ALL',
    center_name VARCHAR(255) NOT NULL DEFAULT 'All Centers',
    modality VARCHAR(100) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- 10. PENDING APPROVALS TABLE (Manager Staging Queue & Super Admin Approvals)
CREATE TABLE IF NOT EXISTS pending_approvals (
    id VARCHAR(100) PRIMARY KEY,
    manager_id VARCHAR(100) NOT NULL,
    manager_name VARCHAR(255) NOT NULL,
    action_type VARCHAR(100) NOT NULL, -- CREATE_CASE, UPDATE_CASE, DELETE_CASE, CREATE_DOCTOR, etc.
    entity_type VARCHAR(100) NOT NULL, -- case, doctor, center, template
    entity_id VARCHAR(100),
    payload JSONB DEFAULT '{}'::jsonb,
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING', -- PENDING, APPROVED, REJECTED
    rejection_reason TEXT,
    created_at VARCHAR(50) NOT NULL,
    reviewed_at VARCHAR(50),
    reviewed_by VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(status);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_manager ON pending_approvals(manager_id);


-- ============================================================================
-- 11. BILLING PERIOD LOCKS (Super Admin monthly close)
-- ============================================================================
CREATE TABLE IF NOT EXISTS billing_period_locks (
    period VARCHAR(7) PRIMARY KEY,              -- YYYY-MM (Asia/Calcutta)
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    locked_at VARCHAR(50),
    locked_by VARCHAR(255)
);

-- ============================================================================
-- 12. INVOICES (monthly center charge / doctor payout — totals immutable when locked)
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoices (
    id VARCHAR(100) PRIMARY KEY,
    party_type VARCHAR(20) NOT NULL,            -- center | doctor
    party_id VARCHAR(100) NOT NULL,
    party_name VARCHAR(255) NOT NULL,
    billing_period VARCHAR(7) NOT NULL,         -- YYYY-MM IST
    status VARCHAR(20) NOT NULL DEFAULT 'pending', -- paid | pending (overdue computed on read)
    locked BOOLEAN NOT NULL DEFAULT FALSE,
    total_amount INTEGER NOT NULL DEFAULT 0,    -- INR whole rupees snapshot
    currency VARCHAR(10) NOT NULL DEFAULT 'INR',
    linked_invoice_id VARCHAR(100),
    created_at VARCHAR(50) NOT NULL,
    updated_at VARCHAR(50) NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_invoices_party ON invoices(party_type, party_id);
CREATE INDEX IF NOT EXISTS idx_invoices_period ON invoices(billing_period);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_locked ON invoices(locked);

-- ============================================================================
-- 13. INVOICE LINE ITEMS (immutable per-study snapshots from sign-off)
-- ============================================================================
CREATE TABLE IF NOT EXISTS invoice_line_items (
    id VARCHAR(100) PRIMARY KEY,
    invoice_id VARCHAR(100) NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    paired_line_item_id VARCHAR(100),
    billing_event_id VARCHAR(100) NOT NULL,
    case_id VARCHAR(100) NOT NULL,
    study_id VARCHAR(100) NOT NULL,
    study_index INTEGER NOT NULL DEFAULT 1,
    service_date VARCHAR(50) NOT NULL,
    description VARCHAR(500) NOT NULL,
    modality VARCHAR(100),
    body_part VARCHAR(255),
    patient_number VARCHAR(100),
    patient_name VARCHAR(255),
    center_id VARCHAR(100),
    center_name VARCHAR(255),
    doctor_id VARCHAR(100),
    doctor_name VARCHAR(255),
    unit_amount INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    amount INTEGER NOT NULL,
    currency VARCHAR(10) NOT NULL DEFAULT 'INR',
    created_at VARCHAR(50) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ili_invoice ON invoice_line_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_ili_case ON invoice_line_items(case_id);
CREATE INDEX IF NOT EXISTS idx_ili_event ON invoice_line_items(billing_event_id);
CREATE INDEX IF NOT EXISTS idx_ili_center ON invoice_line_items(center_id);
CREATE INDEX IF NOT EXISTS idx_ili_doctor ON invoice_line_items(doctor_id);
CREATE INDEX IF NOT EXISTS idx_ili_service_date ON invoice_line_items(service_date);

-- ============================================================================
-- Schema drift fixes (is_urgent used by app models; required for sign-off / billing reads)
-- ============================================================================
ALTER TABLE cases ADD COLUMN IF NOT EXISTS is_urgent BOOLEAN DEFAULT FALSE;
ALTER TABLE studies ADD COLUMN IF NOT EXISTS is_urgent BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_cases_is_urgent ON cases(is_urgent);
CREATE INDEX IF NOT EXISTS idx_studies_is_urgent ON studies(is_urgent);


-- ============================================================================
-- 14. REPORT COMMENTS (Priority 6: flag / reassign / recheck / comment thread)
-- ============================================================================
CREATE TABLE IF NOT EXISTS report_comments (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(100) NOT NULL,
    study_id VARCHAR(100),
    author_user_id INTEGER,
    author_role VARCHAR(50) NOT NULL,
    author_name VARCHAR(255) NOT NULL,
    kind VARCHAR(20) NOT NULL, -- FLAG | REASSIGN | RECHECK | COMMENT
    body TEXT NOT NULL,
    from_doctor_id VARCHAR(100),
    to_doctor_id VARCHAR(100),
    created_at VARCHAR(50) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_report_comments_case ON report_comments(case_id);
CREATE INDEX IF NOT EXISTS idx_report_comments_study ON report_comments(study_id);
CREATE INDEX IF NOT EXISTS idx_report_comments_kind ON report_comments(kind);
CREATE INDEX IF NOT EXISTS idx_report_comments_created ON report_comments(created_at);


-- ============================================================================
-- 15. P6 reassign fix: doctor payout clawback tracking (additive, nullable)
--     status: ACTIVE | CLAWED_BACK (original reversed) | CLAWBACK (negative offset)
-- ============================================================================
ALTER TABLE invoice_line_items ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'ACTIVE';
ALTER TABLE invoice_line_items ADD COLUMN IF NOT EXISTS status_reason TEXT;
ALTER TABLE invoice_line_items ADD COLUMN IF NOT EXISTS offsets_line_item_id VARCHAR(100);
CREATE INDEX IF NOT EXISTS idx_ili_offsets ON invoice_line_items(offsets_line_item_id);

-- P6 close-out: explicit study sequence (1-based bodyParts upload order) used for
-- billing study_index. Additive + nullable; existing rows stay NULL (no backfill).
ALTER TABLE studies ADD COLUMN IF NOT EXISTS sequence_no INTEGER;


-- ============================================================================
-- 16. Public read-only report links (QR on the signed report)
--     One unguessable token per signed study, created at first signing.
--     revoked_at set = link withdrawn by the Super Admin. The app also creates
--     this table at startup (report_links.ensure_report_link_tables).
-- ============================================================================
CREATE TABLE IF NOT EXISTS report_public_links (
    token VARCHAR(64) PRIMARY KEY,
    case_id VARCHAR(100) NOT NULL,
    study_id VARCHAR(100) NOT NULL,
    created_at VARCHAR(50) NOT NULL,
    revoked_at VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS ix_report_public_links_case_id ON report_public_links(case_id);
CREATE INDEX IF NOT EXISTS ix_report_public_links_study_id ON report_public_links(study_id);


-- ============================================================================
-- 17. Per-centre pricing (Super Admin). No row = default rates
--     (centre 30 first / 15 additional, doctor 20 / 10). Used for NEW sign-off
--     billing only; invoice_line_items keep the amount they were billed at.
--     The app also creates this table at startup (billing.ensure_billing_tables).
-- ============================================================================
CREATE TABLE IF NOT EXISTS center_pricing (
    center_id VARCHAR(100) PRIMARY KEY,
    center_first_inr INTEGER NOT NULL,
    center_additional_inr INTEGER NOT NULL,
    doctor_first_inr INTEGER NOT NULL,
    doctor_additional_inr INTEGER NOT NULL,
    updated_at VARCHAR(50),
    updated_by VARCHAR(255)
);


-- ============================================================================
-- 18. Per-user bell notifications (case reassigned to a doctor, new Case
--     Activity message). Read state per user. The app also creates this table
--     at startup (notifications.ensure_user_notification_tables).
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_notifications (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind VARCHAR(40) NOT NULL,
    title VARCHAR(255) NOT NULL,
    body TEXT,
    case_id VARCHAR(100),
    link VARCHAR(255),
    actor_user_id INTEGER,
    actor_name VARCHAR(255),
    created_at VARCHAR(50) NOT NULL,
    read_at VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS ix_user_notifications_user_id ON user_notifications(user_id);
CREATE INDEX IF NOT EXISTS ix_user_notifications_case_id ON user_notifications(case_id);


-- ============================================================================
-- 19. Multi-centre logins: which centres a centre-side login (role CENTER) can
--     use, whether it is that centre's Center Admin, and per-page permissions
--     {"reports","invoices","templates","center_info"}: none | read | write.
--     The app also creates this table at startup (access.ensure_access_tables).
--     Backfill: every existing centre login becomes admin (full rights) of its
--     own centre (users.metadata.centerId). Idempotent.
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_center_access (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    center_id VARCHAR(100) NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    permissions JSONB,
    created_at VARCHAR(50) NOT NULL,
    created_by VARCHAR(255),
    CONSTRAINT uq_user_center_access_user_center UNIQUE (user_id, center_id)
);
CREATE INDEX IF NOT EXISTS ix_user_center_access_user_id ON user_center_access(user_id);
CREATE INDEX IF NOT EXISTS ix_user_center_access_center_id ON user_center_access(center_id);

INSERT INTO user_center_access (user_id, center_id, is_admin, permissions, created_at, created_by)
SELECT u.id,
       u.metadata->>'centerId',
       TRUE,
       '{"reports":"write","invoices":"write","templates":"write","center_info":"write"}'::jsonb,
       to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       'migration'
FROM users u
WHERE u.role = 'CENTER'
  AND COALESCE(u.metadata->>'centerId', '') <> ''
  AND NOT EXISTS (SELECT 1 FROM user_center_access a WHERE a.user_id = u.id)
ON CONFLICT (user_id, center_id) DO NOTHING;


-- ============================================================================
-- 20. PACS image marks (arrows) drawn by doctors, persisted per case image.
--     image_key = stored file path (after /api/files/) or "img-<index>".
--     data = normalised 0..1000 image coordinates {"x1","y1","x2","y2"}.
--     The app also creates this table at startup (access.ensure_access_tables).
-- ============================================================================
CREATE TABLE IF NOT EXISTS image_annotations (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(100) NOT NULL,
    image_key VARCHAR(500) NOT NULL,
    kind VARCHAR(20) NOT NULL DEFAULT 'arrow',
    data JSONB NOT NULL,
    author_user_id INTEGER,
    author_name VARCHAR(255),
    author_role VARCHAR(50),
    created_at VARCHAR(50) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_image_annotations_case_id ON image_annotations(case_id);
