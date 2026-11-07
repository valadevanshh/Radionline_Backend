"""Billable X-ray view count.

Prices use the centre rate card (first unit / each additional unit). The unit is
an explicit study view, never an uploaded image file.

Authoritative inputs, in order:
1. study metadata "views" — the labels the upload form selected (StudyUpload.views).
2. Otherwise, view labels explicitly written into the stored body-part name
   ("KNEE AP/LATERAL", "CHEST AP SUPINE", "CHEST PA & LATERAL").
3. If neither source yields an explicit view, the study counts as one unit.
   Image-file rows are not consulted.

AP/LAT and AP/LATERAL are the same two views. LAT and LATERAL are one view.
A single catalog label with no separator (AP SUPINE, SKYLINE VIEW) stays one view.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

_BODY_PART_NAMES = """
    Abdomen
    Kub
    Chest
    Left Chest
    Right Chest
    Cervical Spine
    Dorsal Spine
    Lumbar Spine
    Dorso-Lumbar Spine
    PBH / Hip
    Left Hip
    Right Hip
    Sacroiliac Joints
    Both Knee
    Left Knee
    Right Knee
    Left Ankle
    Right Ankle
    Both Ankle
    Left Shoulder
    Right Shoulder
    Both Shoulder
    Left Elbow
    Right Elbow
    Both Elbow
    Left Wrist
    Right Wrist
    Both Wrist
    Paranasal (PNS)
    Left Arm
    Right Arm
    Both Arms
    Coccyx
    Finger
    Both Feet
    Left Foot
    Right Foot
    Left Forearm
    Right Forearm
    Both Forearms
    Left Hand
    Right Hand
    Both Hands
    Left Leg
    Right Leg
    Both Legs
    Left Mastoid
    Right Mastoid
    Both Mastoids
    Nasal Bones
    Skull
    Left Thigh
    Right Thigh
    Both Thighs
    Left Thumb
    Right Thumb
    Both Thumbs
    Cologram
    HSG
    MCU
    RGU
    DRU
    IVP
    Barium
    Neck
    Adenoids
    Apicogram
    Sinogram
    Mandible
    Left TM Joint
    Right TM Joint
    Both TM Joints
    Mammography
    Abdomen Barium Enema
    Abdomen Gallbladder
    Abdomen HSG (Hysterosalpingography)
    Abdomen Intestine
    Abdomen IVP (Intravenous Pyelogram)
    Abdomen Pediatric
    Ankle
    Calcaneus
    Left Calcaneus
    Right Calcaneus
    Both Calcaneus
    Both Foot
    Foot
    Big / First Toe Left Foot
    Big / First Toe Right Foot
    Big / First Toe both Foot / Feet
    Long / Second Toe Left Foot
    Long / Second Toe Right Foot
    Long / Second Toe both Foot / Feet
    Middle / Third Toe Left Foot
    Middle / Third Toe Right Foot
    Middle / Third Toe both Foot / Feet
    Ring / Fourth Toe Left Foot
    Ring / Fourth Toe Right Foot
    Ring / Fourth Toe both Foot / Feet
    Little / Fifth Toe Left Foot
    Little / Fifth Toe Right Foot
    Little / Fifth Toe both Foot / Feet
    Toe Left Foot
    Toe Right Foot
    Toe both Foot / Feet
    Toe
    Heel
    Left Heel
    Right Heel
    Both Heels
    Skull Mandible
    Skull Nasal
    Nasal Septum
    Left Skull
    Right Skull
    Skull Cranium
    Skull Facial
    Skull Sinus
    Skull Zygoma
    Elbow
    Hand
    Left Hand BMD (Bone Mineral Density)
    Right Hand BMD (Bone Mineral Density)
    Cholangiogram
    Both Hands BMD (Bone Mineral Density)
    Hand BMD (Bone Mineral Density)
    Thumb Finger Left Hand
    Index Finger Left Hand
    Middle Finger Left Hand
    Ring Finger Left Hand
    Little Finger Left Hand
    Thumb Finger Right Hand
    Index Finger Right Hand
    Middle Finger Right Hand
    Ring Finger Right Hand
    Little Finger Right Hand
    Thumb Finger Both Hands
    Index Finger Both Hands
    Middle Finger Both Hands
    Ring Finger Both Hands
    Little Finger Both Hands
    Wrist
    Scaphoid
    Scaphoid Left Wrist
    Scaphoid Right Wrist
    Scaphoid Both Wrists
    Both Hips
    Multiple Studies
    Pelvis and Both Hips (PBH)
    Pelvis
    Knee
    Knee Patella
    Right Orbit
    Left Orbit
    Orbit
    Femur
    Left Femur
    Right Femur
    Both Femurs
    Pharynx / larynx
    Fibula / Tibia
    Left Fibula / Tibia
    STN (Soft Tissue Neck)
    Right Fibula / Tibia
    Both Fibula / Tibia
    Nasopharynx
    LLI Leg Upright (Long Length Imaging)
    LLI Leg Supine (Long Length Imaging)
    Mastoid Process
    Left Shoulder Clavicle
    Right Shoulder Clavicle
    Both Shoulder Clavicles
    Right Shoulder Scapula
    Left Shoulder Scapula
    Both Shoulder Scapula
    Dental Panoramic Cranium
    Dental CEPH (Cephalometric Analysis) Cranium
    Both SM Bone (Sesamoid)
    Left SM Bone (Sesamoid)
    Right SM Bone (Sesamoid)
    Both Humerus
    LLI Spine Supine (Long Length Imaging)
    LLI Spine Uprght (Long Length Imaging)
    Right Humerus
    Left Humerus
    Spine Coccyx
    Forearm
    Spine Sacrum
    Ribs Right Chest
    Thoracic Spine
    Ribs Left Chest
    TL (Thoracolumbar) Spine
    Thorax Sternum
    Thorax Ribs (Upper)
    Sacrum Coccyx
    Thorax Ribs (Lower)
    Thorax
    BMFT (Barium Meal Follow Through)
    Contrast Enema
    Esophagus
    Scanogram
    Chest Portable
    Scanogram Both Lower Limbs
    Chest Pediatric
    Scanogram Both Lower Limbs Standing
    Scanogram Left Lower Limb
    Scanogram Left Lower Limb Standing
    Scanogram Right Lower Limb
    Scanogram Right Lower Limb Standing
    Distal Colostogram
    Fistulogram
    Loopogram Procedure
    Retrograde Urethrography (RUG)
    Ascending Urethrography (ASU)
    Urethrography (Urethrogram)
    Micturating Cystourethrography (MCU )
    Voiding cystourethrography (VCUG)
    Abdomen Standing
    SI Joints (Sacroiliac)
    Left Knee Standing
    Both AC Joint (Acromio Clavicular)
    Right Knee Standing
    Right AC Joint (Acromio Clavicular)
    Left AC Joint (Acromio Clavicular)
    LS Spine (Lumbo Sacral)
    Thoracic Inlet
    Both Knee Standing
    Bilateral Styloid Process
    Left Styloid Process
    Right Styloid Process
    Left Ankle and Foot
    Right Ankle and Foot
    Both Ankel and Foot
    Both Lower Limbs
    Both Lower Limbs Standing (Erect)
    Left Lower Limbs
    Left Lower Limbs Standing (Erect)
    Right Lower Limbs
    Right Lower Limbs Standing (Erect)
    Left Knee and Femur
    Left Knee, Hip and Femur
    Left Hip and Femur
    Right Knee and Femur
    Right Knee, Hip and Femur
    Right Hip and Femur
    Both Knee and Femur
    Both Knee, Hip and Femur
    Both Hip and Femur
    Chest and Abdomen
    Full Body
    Left Ankle and Fibula / Tibia
    Right Ankle and Fibula / Tibia
    Both Ankle and Fibula / Tibia
    Left Shoulder and Humerus
    Right Shoulder and Humerus
    Both Shoulder and Humerus
    Left Hand and Wrist
    Right Hand and Wrist
    Both Hands and Wrist
    Lumbar Spine and PBH / HIP
    Left Shoulder and Cervical Spine
    Right Shoulder and Cervical Spine
    Both Shoulder and Cervical Spine
    Skull Nasal and Mandible
    Bladder
    Both Hands with Wrists
    Foot with Ankle
    Right Foot With Ankle
    Left Foot with Ankle
    Infantogram
    IVHRTOGRAM
    INVERTOGRAM
    ECG
    OPG
    EEG
    Animal Xray
    Cholongiogram
    Dacryocystography
    RIBS
    STERNUM
    PNS
    NASAL BONE
    SOFT TISSUE NECK
    TMJ
    MASTOID
    DORSO LUMBAR SPINE
    LS SPINE
    SACRUM
    SACROCOCCYX
    WHOLE SPINE
    PELVIS WITH BOTH HIPS
    HIP
    SI JOINTS
    LEG
    TOES
    CLAVICLE
    SHOULDER
    HUMERUS
    THUMB
"""

_VIEW_NAMES = """
    PA
    LATERAL
    AP ERECT
    SUPINE
    LORDOTIC
    LATERAL DECUBITUS
    EXPIRATORY CHEST RADIOGRAPH
    STERNUM LATERAL
    STERNUM OBLIQUE
    RIBS AP
    RIBS PA
    RIBS OBLIQUE
    AP SUPINE
    PA SUPINE
    PA ERECT
    DORSAL DECUBITUS
    PA PRONE
    OBLIQUE
    AP
    AP OBLIQUE
    PA OBLIQUE
    CERVICOTHORACIC (SWIMMER'S VIEW)
    AP OPEN MOUTH ODONTOID
    AP CLOSED MOUTH ODONTOID (FUCHS VIEW)
    LATERAL FLEXION AND EXTENSION
    OUTLET
    INLET
    OBLIQUE ACETABULAR (JUDET VIEW)
    FLAMINGO
    CLEMENTS-NAKAYAMA
    FROG LEG
    DUNN
    VON ROSEN
    HORIZONTAL BEAM LATERAL
    AP WEIGHT-BEARING
    INTERCONDYLAR
    ROSENBERG
    SKYLINE LAURINE
    SKYLINE MERCHANT
    AP MORTISE
    AP STRESS
    INTERNAL ROTATION
    EXTERNAL ROTATION
    SUPEROINFERIOR AXIAL
    INFEROSUPERIOR AXIAL
    MODIFIED TRAUMA AXIAL
    SUPINE LATERAL
    MODIFIED SUPINE LATERAL
    Y LATERAL
    AP GLENOID (GRASHEY VIEW)
    APICAL OBLIQUE (GARTH VIEW)
    HUMERUS (NECK) AP
    HUMERUS AXIAL (BICIPITAL GROOVE) (FISK VIEW)
    STRYKER NOTCH
    SUPRACONDYLAR AP
    ACUTE FLEXION AP
    COYLE'S
    EXTERNAL OBLIQUE
    MEDIAL OBLIQUE
    CARPAL TUNNEL
    TRAPEZIUM
    PISIFORM
    PA RADIAL DEVIATION
    CARPAL BRIDGE
    PA ANGLED (CALDWELL VIEW)
    PARIETOCANTHAL (WATERS VIEW)
    TRANSORAL PARIETOCANTHAL (OPEN MOUTH WATERS VIEW)
    SUBMENTOVERTEX (SMV)
    DP
    WEIGHTBEARING AP
    WEIGHTBEARING LATERAL
    DP OBLIQUE
    BALL-CATCHER (NORGAARD VIEW)
    WATERS
    AXIAL
    AP AXIAL(TOWNE VIEW)
    PA AXIAL (CALDWELL VIEW)
    OCCIPITOMENTAL (WATERS VIEW)
    ACANTHIOPARIETAL (REVERSE WATERS VIEW)
    OCCIPITOMENTAL 30º (TITTERINGTON VIEW)
    AP/PA
    AXIOLATERAL
    AP AXIAL
    AXIOLATERAL OBLIQUE
    STENVERS
    MODIFIED STENVERS
    AP DECUBITUS
    LATERAL XTABLE
    SPECIAL
    NOT AVAILABLE (NA)
    TANGENTIAL
    DORSOPLANTAR
    TOWNE
    CALDWELL
    SCAPHOID
    PA ULNAR DEVIATION
    MULTI VIEW 1
    MULTI VIEW 2
    MULTI VIEW 3
    MULTI VIEW 4
    MULTI VIEW 5
    MULTI VIEW 6
    SKYLINE LAURIN
    OM
    AXILLARY
    AP CEPHALIC
    LATERAL OBLIQUE
    FLEXION
    EXTENSION
    PANORAMIC
    CEPHALOGRAPHIC
    ODONTOID
    SWIMMER
    PA-Flip Horizontal
    AP SUPINE (NEONATAL)
    CROSS TABLE LATERAL
    ZANCA
    OPEN MOUTH
    CLOSE MOUTH
    PA Flip Horizontal
    ECG
    OPG
    EEG
    TUNNEL
    SUNRISE
    Both Saltzman View
    SALTZMAN
    LAT
    ERECT
    WATERS VIEW
    CALDWELL VIEW
    SKYLINE VIEW
    STANDING AP
    Y VIEW
    VIEWS
    PA & LATERAL
    AP/LAT
    AP/LATERAL
    AP/OBLIQUE
    PA/OBLIQUE
    FLEXION/EXTENSION
    LATERAL/AXIAL
    AP/AXIAL
    ERECT/SUPINE
"""

_SIDE_RE = re.compile(r"\s*\((LEFT|RIGHT|BOTH)\)\s*$", re.I)

# Whole-token synonyms only. "LATERAL DECUBITUS" is not the same view as "LAT".
_SYNONYMS = {
    "LAT": "LAT",
    "LATERAL": "LAT",
    "OBL": "OBLIQUE",
    "OBLIQUE": "OBLIQUE",
}


def _norm(value: str) -> str:
    text = str(value or "").upper().replace("º", "°").replace("—", "-")
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s*/\s*", " / ", text)
    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _canon_token(token: str) -> str:
    token = _norm(token)
    return _SYNONYMS.get(token, token)


def _load_names(block: str) -> List[str]:
    return [ln.strip() for ln in block.splitlines() if ln.strip()]


_BODY_SORTED = tuple(sorted({_norm(n) for n in _load_names(_BODY_PART_NAMES)}, key=len, reverse=True))
_KNOWN_VIEWS = frozenset(_canon_token(n) for n in _load_names(_VIEW_NAMES))


def _split_combo(label: str) -> List[str]:
    """Split on / or & outside parentheses. AP SUPINE (no separator) stays one label."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in label:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and ch in "/&":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def _expand_label(label: str) -> List[str]:
    """One selected label -> one or more canonical views.

    A slash or ampersand combination counts as several views only when every
    piece is a known projection (AP/LAT, AP/LATERAL, PA & LATERAL). Otherwise
    the whole label is one explicit view, including a custom name.
    """
    text = _norm(label)
    if not text:
        return []
    pieces = _split_combo(text)
    if len(pieces) >= 2:
        canon = [_canon_token(p) for p in pieces]
        if all(c in _KNOWN_VIEWS for c in canon):
            return _unique(canon)
    return [_canon_token(text)]


def _unique(labels: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for label in labels:
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
    return out


def _body_prefix(text: str) -> str:
    for name in _BODY_SORTED:
        if text == name or text.startswith(name + " "):
            return name
    return ""


def _parse_view_expression(expr: str) -> List[str]:
    expr = _norm(expr)
    if not expr:
        return []
    # Whole remainder is one known projection, including multi-word names
    # ("AP SUPINE"). Spaces are not extra views.
    whole = _canon_token(expr)
    if whole in _KNOWN_VIEWS and len(_split_combo(expr)) == 1:
        return [whole]
    pieces = _split_combo(expr)
    if len(pieces) >= 2:
        canon = [_canon_token(p) for p in pieces]
        if all(c in _KNOWN_VIEWS for c in canon):
            return _unique(canon)
    return []


def infer_views_from_body_part(body_part: str) -> List[str]:
    """Views explicitly present in a stored study name. [] when none are explicit."""
    text = _norm(body_part)
    text = _SIDE_RE.sub("", text).strip()
    if not text:
        return []
    prefix = _body_prefix(text)
    if not prefix:
        return _parse_view_expression(text)
    remainder = text[len(prefix):].strip()
    if not remainder:
        return []
    return _parse_view_expression(remainder)


def explicit_view_labels(views: Optional[Sequence]) -> List[str]:
    if not isinstance(views, (list, tuple)):
        return []
    labels: List[str] = []
    for item in views:
        if not isinstance(item, str):
            continue
        labels.extend(_expand_label(item))
    return _unique(labels)


def billable_view_labels(*, body_part: str = "", explicit_views: Optional[Sequence] = None) -> List[str]:
    """Ordered unique views to price for one study.

    There is no image-count argument. Missing explicit metadata counts as one unit.
    """
    chosen = explicit_view_labels(explicit_views)
    if chosen:
        return chosen
    inferred = infer_views_from_body_part(body_part)
    if inferred:
        return inferred
    return ["STUDY"]


def billable_view_count(*, body_part: str = "", explicit_views: Optional[Sequence] = None) -> int:
    return len(billable_view_labels(body_part=body_part, explicit_views=explicit_views))
