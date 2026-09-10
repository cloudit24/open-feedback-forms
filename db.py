"""
MariaDB layer for the feedback app.

Everything the fan-facing form and the admin panel need to store lives
here: the question list (`form_fields`) and the submissions (`feedback`).
Schema is created automatically the first time a connection succeeds —
nothing to run by hand. `extra_fields` is stored as JSON text rather than
one real column per question, so an admin adding/renaming/removing a
question never requires an ALTER TABLE from a web request.

Requires an empty database to already exist and a user with rights on it
(CREATE, ALTER, SELECT, INSERT, UPDATE on that one database is enough —
this code never issues CREATE DATABASE, since that's a bigger grant than
a feedback form should need to ask for).
"""

import json
import threading
from datetime import datetime, timezone

import mysql.connector
from mysql.connector import pooling

_UNSET = object()   # distinguishes "not provided" from "explicitly set to None" in optional update params
_pool = None
_pool_lock = threading.Lock()
_last_error = None

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS forms (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        name          VARCHAR(120) NOT NULL,
        slug          VARCHAR(64)  NOT NULL UNIQUE,
        port          INT          NOT NULL UNIQUE,
        active        TINYINT(1) NOT NULL DEFAULT 1,
        org_name      VARCHAR(120) NULL,
        logo_filename VARCHAR(120) NULL,
        primary_color VARCHAR(7)  NULL,
        ink_color     VARCHAR(7)  NULL,
        lang_en       TINYINT(1) NOT NULL DEFAULT 1,
        lang_ar       TINYINT(1) NOT NULL DEFAULT 1,
        subtitle_en   VARCHAR(300) NULL,
        subtitle_ar   VARCHAR(300) NULL,
        enabled_extra_langs_json LONGTEXT NULL,
        expiry_date   DATE NULL,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS form_fields (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        form_id       INT          NOT NULL DEFAULT 1,
        field_key     VARCHAR(64)  NOT NULL,
        label_en      VARCHAR(200) NOT NULL,
        label_ar      VARCHAR(200) NOT NULL,
        labels_extra_json LONGTEXT NULL,
        field_type    ENUM('text','email','tel','textarea','select','rating','checkbox') NOT NULL,
        options_json  LONGTEXT NULL,
        required      TINYINT(1) NOT NULL DEFAULT 0,
        sort_order    INT NOT NULL DEFAULT 0,
        active        TINYINT(1) NOT NULL DEFAULT 1,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uniq_form_field (form_id, field_key),
        INDEX idx_form (form_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS field_keys (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        field_key     VARCHAR(64)  NOT NULL UNIQUE,
        label_en      VARCHAR(200) NOT NULL,
        label_ar      VARCHAR(200) NOT NULL,
        labels_extra_json LONGTEXT NULL,
        field_type    ENUM('text','email','tel','textarea','select','rating','checkbox') NOT NULL,
        options_json  LONGTEXT NULL,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        form_id       INT          NOT NULL DEFAULT 1,
        reference     VARCHAR(20)  NOT NULL UNIQUE,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        first_name    VARCHAR(60)  NOT NULL,
        last_name     VARCHAR(60)  NOT NULL,
        email         VARCHAR(120) NOT NULL,
        consent       TINYINT(1) NOT NULL DEFAULT 1,
        language      VARCHAR(5),
        extra_fields  LONGTEXT NOT NULL,
        ip            VARCHAR(45),
        user_agent    VARCHAR(250),
        status        VARCHAR(20) NOT NULL DEFAULT 'new',
        INDEX idx_created (created_at),
        INDEX idx_form_created (form_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_roles (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        name          VARCHAR(60)  NOT NULL UNIQUE,
        permissions_json LONGTEXT NOT NULL,
        applies_to_all_forms TINYINT(1) NOT NULL DEFAULT 0,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_users (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        username      VARCHAR(60)  NOT NULL UNIQUE,
        password_hash VARCHAR(128) NOT NULL,
        salt          VARCHAR(64)  NOT NULL,
        role_id       INT          NOT NULL,
        active        TINYINT(1) NOT NULL DEFAULT 1,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (role_id) REFERENCES admin_roles(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS admin_user_forms (
        user_id       INT NOT NULL,
        form_id       INT NOT NULL,
        PRIMARY KEY (user_id, form_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_rules (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        form_id       INT NOT NULL,
        trigger_type  ENUM('new_submission','daily_digest','form_expiry') NOT NULL,
        channel       ENUM('email','telegram') NOT NULL,
        destination   VARCHAR(200) NOT NULL,
        enabled       TINYINT(1) NOT NULL DEFAULT 1,
        created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_form_trigger (form_id, trigger_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

# Best-effort migration for a database that already has the pre-multi-form
# schema (no form_id columns). Every statement is wrapped individually and
# failures are swallowed, since on a fresh database every one of these is
# already satisfied by SCHEMA_SQL above and would just error as a no-op
# ("duplicate column", "duplicate key name", ...).
MIGRATE_SQL = [
    "ALTER TABLE form_fields ADD COLUMN IF NOT EXISTS form_id INT NOT NULL DEFAULT 1 AFTER id",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS form_id INT NOT NULL DEFAULT 1 AFTER id",
    "ALTER TABLE form_fields DROP INDEX field_key",
    "ALTER TABLE form_fields ADD UNIQUE KEY uniq_form_field (form_id, field_key)",
    "ALTER TABLE form_fields ADD INDEX idx_form (form_id)",
    "ALTER TABLE feedback ADD INDEX idx_form_created (form_id, created_at)",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS org_name VARCHAR(120) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS logo_filename VARCHAR(120) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS primary_color VARCHAR(7) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS ink_color VARCHAR(7) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS lang_en TINYINT(1) NOT NULL DEFAULT 1",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS lang_ar TINYINT(1) NOT NULL DEFAULT 1",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS subtitle_en VARCHAR(300) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS subtitle_ar VARCHAR(300) NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS enabled_extra_langs_json LONGTEXT NULL",
    "ALTER TABLE form_fields ADD COLUMN IF NOT EXISTS labels_extra_json LONGTEXT NULL",
    "ALTER TABLE field_keys ADD COLUMN IF NOT EXISTS labels_extra_json LONGTEXT NULL",
    "ALTER TABLE forms ADD COLUMN IF NOT EXISTS expiry_date DATE NULL",
]

# Ported straight from the old hardcoded index.html, so the form looks and
# behaves exactly the same on the very first run — an admin only sees a
# change once they actually go and make one.
_NAT_OPTIONS = [
    ["AE", "United Arab Emirates", "الإمارات العربية المتحدة"], ["SA", "Saudi Arabia", "السعودية"],
    ["KW", "Kuwait", "الكويت"], ["QA", "Qatar", "قطر"], ["BH", "Bahrain", "البحرين"], ["OM", "Oman", "عُمان"],
    ["EG", "Egypt", "مصر"], ["JO", "Jordan", "الأردن"], ["SY", "Syria", "سوريا"], ["LB", "Lebanon", "لبنان"],
    ["PS", "Palestine", "فلسطين"], ["IQ", "Iraq", "العراق"], ["YE", "Yemen", "اليمن"], ["SD", "Sudan", "السودان"],
    ["LY", "Libya", "ليبيا"], ["TN", "Tunisia", "تونس"], ["DZ", "Algeria", "الجزائر"], ["MA", "Morocco", "المغرب"],
    ["MR", "Mauritania", "موريتانيا"], ["SO", "Somalia", "الصومال"], ["DJ", "Djibouti", "جيبوتي"],
    ["KM", "Comoros", "جزر القمر"],
    ["IN", "India", "الهند"], ["PK", "Pakistan", "باكستان"], ["BD", "Bangladesh", "بنغلاديش"],
    ["LK", "Sri Lanka", "سريلانكا"], ["NP", "Nepal", "نيبال"], ["AF", "Afghanistan", "أفغانستان"],
    ["IR", "Iran", "إيران"], ["PH", "Philippines", "الفلبين"], ["ID", "Indonesia", "إندونيسيا"],
    ["MY", "Malaysia", "ماليزيا"], ["TH", "Thailand", "تايلاند"], ["VN", "Vietnam", "فيتنام"],
    ["CN", "China", "الصين"], ["JP", "Japan", "اليابان"], ["KR", "South Korea", "كوريا الجنوبية"],
    ["TR", "Turkey", "تركيا"], ["UZ", "Uzbekistan", "أوزبكستان"], ["KZ", "Kazakhstan", "كازاخستان"],
    ["AZ", "Azerbaijan", "أذربيجان"],
    ["GB", "United Kingdom", "المملكة المتحدة"], ["IE", "Ireland", "أيرلندا"], ["FR", "France", "فرنسا"],
    ["DE", "Germany", "ألمانيا"], ["IT", "Italy", "إيطاليا"], ["ES", "Spain", "إسبانيا"],
    ["PT", "Portugal", "البرتغال"], ["NL", "Netherlands", "هولندا"], ["BE", "Belgium", "بلجيكا"],
    ["CH", "Switzerland", "سويسرا"], ["AT", "Austria", "النمسا"], ["SE", "Sweden", "السويد"],
    ["NO", "Norway", "النرويج"], ["DK", "Denmark", "الدنمارك"], ["FI", "Finland", "فنلندا"],
    ["PL", "Poland", "بولندا"], ["RO", "Romania", "رومانيا"], ["RU", "Russia", "روسيا"],
    ["UA", "Ukraine", "أوكرانيا"], ["GR", "Greece", "اليونان"], ["RS", "Serbia", "صربيا"],
    ["HR", "Croatia", "كرواتيا"],
    ["US", "United States", "الولايات المتحدة"], ["CA", "Canada", "كندا"], ["MX", "Mexico", "المكسيك"],
    ["BR", "Brazil", "البرازيل"], ["AR", "Argentina", "الأرجنتين"], ["CO", "Colombia", "كولومبيا"],
    ["CL", "Chile", "تشيلي"], ["PE", "Peru", "بيرو"], ["UY", "Uruguay", "الأوروغواي"],
    ["VE", "Venezuela", "فنزويلا"],
    ["NG", "Nigeria", "نيجيريا"], ["GH", "Ghana", "غانا"], ["KE", "Kenya", "كينيا"],
    ["ET", "Ethiopia", "إثيوبيا"], ["UG", "Uganda", "أوغندا"], ["TZ", "Tanzania", "تنزانيا"],
    ["CM", "Cameroon", "الكاميرون"], ["SN", "Senegal", "السنغال"], ["CI", "Ivory Coast", "ساحل العاج"],
    ["ZA", "South Africa", "جنوب أفريقيا"], ["ZW", "Zimbabwe", "زيمبابوي"],
    ["AU", "Australia", "أستراليا"], ["NZ", "New Zealand", "نيوزيلندا"],
    ["OTHER", "Other", "دولة أخرى"],
]
_GENDER_OPTIONS = [
    ["male", "Male", "ذكر"],
    ["female", "Female", "أنثى"],
    ["na", "Prefer not to say", "أفضل عدم الإفصاح"],
]
_AGE_OPTIONS = [
    ["u18", "Under 18", "أقل من 18"],
    ["18_24", "18–24", "24–18"],
    ["25_34", "25–34", "34–25"],
    ["35_44", "35–44", "44–35"],
    ["45_54", "45–54", "54–45"],
    ["55p", "55+", "55 فأكثر"],
]
_ATTENDANCE_OPTIONS = [
    ["first_time", "This is my first match", "هذه أول مباراة لي"],
    ["occasionally", "Occasionally", "من حين لآخر"],
    ["several_per_season", "Several times per season", "عدة مرات في الموسم"],
    ["most_home", "Most home matches", "معظم المباريات على أرضنا"],
    ["almost_every", "Almost every home match", "كل المباريات تقريبًا"],
]
_HEARD_OPTIONS = [
    ["social", "Social media", "مواقع التواصل الاجتماعي"],
    ["website_app", "Club website or app", "موقع أو تطبيق النادي"],
    ["friends_family", "Friends or family", "الأصدقاء أو العائلة"],
    ["whatsapp", "WhatsApp", "واتساب"],
    ["other", "Other", "أخرى"],
]
_EXPERIENCE_SCALE = [
    ["excellent", "Excellent", "ممتاز"],
    ["good", "Good", "جيد"],
    ["average", "Average", "متوسط"],
    ["poor", "Poor", "ضعيف"],
    ["very_poor", "Very poor", "ضعيف جدًا"],
    ["na", "N/A", "لا ينطبق"],
]
_NPS_OPTIONS = [[str(n), str(n), str(n)] for n in range(11)]


def _opts(rows):
    return json.dumps([{"value": r[0], "label_en": r[1], "label_ar": r[2]} for r in rows])


DEFAULT_FIELDS = [
    # field_key,             label_en,                                                   label_ar,                                          type,       options,                   required
    ("phone",                "Mobile number",                                            "رقم الهاتف المتحرك",                              "tel",      None,                      1),
    ("gender",               "Gender",                                                    "الجنس",                                            "select",   _opts(_GENDER_OPTIONS),    1),
    ("age_group",            "Age group",                                                 "الفئة العمرية",                                    "select",   _opts(_AGE_OPTIONS),       1),
    ("nationality",          "Nationality",                                               "الجنسية",                                          "select",   _opts(_NAT_OPTIONS),       1),
    ("attendance_frequency", "How often do you attend our home matches?",                  "كم مرة تحضر مباريات النادي على أرضنا؟",             "select",   _opts(_ATTENDANCE_OPTIONS), 1),
    ("heard_about",          "How did you hear about today's match?",                     "كيف علمت بمباراة اليوم؟",                          "select",   _opts(_HEARD_OPTIONS),     1),
    ("exp_entry",            "Stadium entry & access",                                    "الدخول إلى الاستاد",                               "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("exp_seating",          "Seating & facilities",                                      "المقاعد والمرافق",                                 "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("exp_cleanliness",      "Cleanliness",                                               "النظافة",                                          "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("exp_food",             "Food & beverage",                                           "المأكولات والمشروبات",                            "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("exp_atmosphere",       "Atmosphere & entertainment",                                "الأجواء والترفيه",                                 "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("exp_staff",            "Staff & customer service",                                  "الموظفون وخدمة العملاء",                          "select",   _opts(_EXPERIENCE_SCALE),  1),
    ("overall_satisfaction", "Overall, how satisfied were you with today's matchday?",    "بشكل عام، ما مدى رضاك عن يوم المباراة؟",           "rating",   None,                      1),
    ("nps",                  "How likely are you to recommend us to a friend or family member? (0 = not likely, 10 = extremely likely)", "ما مدى احتمالية أن توصي بنا لصديق أو أحد أفراد العائلة؟ (0 = غير محتمل، 10 = محتمل جدًا)", "select", _opts(_NPS_OPTIONS), 1),
    ("feedback_message",     "What's the one thing we could improve for your next visit?", "ما الشيء الوحيد الذي يمكننا تحسينه لزيارتك القادمة؟", "textarea", None,                    0),
    ("marketing_optin",      "Yes, keep me updated with club news, match updates, ticket offers and exclusive fan experiences.", "نعم، أرغب بتلقي أخبار النادي وتحديثات المباريات وعروض التذاكر وتجارب حصرية للمشجعين.", "checkbox", None, 0),
]


def list_field_keys():
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM field_keys ORDER BY label_en")
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["options"] = json.loads(r["options_json"]) if r["options_json"] else None
            r["labels_extra"] = json.loads(r["labels_extra_json"]) if r["labels_extra_json"] else {}
            del r["options_json"]; del r["labels_extra_json"]
        return rows
    finally:
        conn.close()


def get_field_key(key_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM field_keys WHERE id=%s", (key_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row["options"] = json.loads(row["options_json"]) if row["options_json"] else None
            row["labels_extra"] = json.loads(row["labels_extra_json"]) if row["labels_extra_json"] else {}
            del row["options_json"]; del row["labels_extra_json"]
        return row
    finally:
        conn.close()


def create_field_key(data):
    conn = _conn()
    try:
        cur = conn.cursor()
        opts = json.dumps(data["options"]) if data.get("options") else None
        extra = json.dumps(data["labels_extra"]) if data.get("labels_extra") else None
        cur.execute(
            """INSERT INTO field_keys (field_key, label_en, label_ar, labels_extra_json, field_type, options_json)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (data["field_key"], data["label_en"], data["label_ar"], extra, data["field_type"], opts))
        conn.commit()
        new_id = cur.lastrowid
        cur.close()
        return new_id
    finally:
        conn.close()


def update_field_key(key_id, data):
    conn = _conn()
    try:
        cur = conn.cursor()
        opts = json.dumps(data["options"]) if data.get("options") else None
        extra = json.dumps(data["labels_extra"]) if data.get("labels_extra") else None
        cur.execute(
            "UPDATE field_keys SET label_en=%s, label_ar=%s, labels_extra_json=%s, field_type=%s, options_json=%s WHERE id=%s",
            (data["label_en"], data["label_ar"], extra, data["field_type"], opts, key_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_field_key(key_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM field_keys WHERE id=%s", (key_id,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def dashboard_summary(filters=None):
    """Core metrics for the Dashboard tab: totals, per-form and per-language
    breakdowns, and a daily submissions trend — computed from the fixed
    `feedback` columns only, so it works the same regardless of which custom
    questions any given form asks."""
    filters = filters or {}
    where, params = _submission_filter_sql(filters, prefix="f.")
    base = " FROM feedback f JOIN forms fm ON fm.id = f.form_id " + where

    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)

        cur.execute("SELECT COUNT(*) AS n" + base, params)
        total = cur.fetchone()["n"]

        cur.execute(
            "SELECT fm.id, fm.name, COUNT(*) AS n" + base + " GROUP BY fm.id, fm.name ORDER BY n DESC", params)
        by_form = cur.fetchall()

        cur.execute(
            "SELECT COALESCE(NULLIF(f.language,''),'—') AS lang, COUNT(*) AS n" + base + " GROUP BY lang", params)
        by_language = cur.fetchall()

        cur.execute(
            "SELECT DATE(f.created_at) AS d, COUNT(*) AS n" + base + " GROUP BY DATE(f.created_at) ORDER BY d", params)
        trend = [{"date": str(r["d"]), "n": r["n"]} for r in cur.fetchall()]

        cur.close()
        return {"total": total, "byForm": by_form, "byLanguage": by_language, "trend": trend}
    finally:
        conn.close()


class DBError(Exception):
    pass


def _make_pool(db_cfg):
    return pooling.MySQLConnectionPool(
        pool_name="offeedback_pool_%d" % id(db_cfg),
        pool_size=5,
        host=db_cfg["host"],
        port=int(db_cfg.get("port") or 3306),
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        connection_timeout=10,
        charset="utf8mb4",
        autocommit=False,
    )


def _create_schema(conn):
    cur = conn.cursor()
    for stmt in SCHEMA_SQL:
        cur.execute(stmt)
    cur.close()


def _migrate_schema(conn):
    cur = conn.cursor()
    for stmt in MIGRATE_SQL:
        try:
            cur.execute(stmt)
        except mysql.connector.Error:
            pass
    conn.commit()
    cur.close()


def _seed_fields(conn, form_id, cur=None):
    own_cur = cur is None
    if own_cur:
        cur = conn.cursor()
    for i, (key, en, ar, ftype, options, required) in enumerate(DEFAULT_FIELDS):
        cur.execute(
            """INSERT INTO form_fields
               (form_id, field_key, label_en, label_ar, field_type, options_json, required, sort_order, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)""",
            (form_id, key, en, ar, ftype, options, required, i))
    if own_cur:
        cur.close()


def _seed_field_key_library_if_empty(conn):
    """The admin-managed catalog of field keys (Field keys tab). Seeded once
    from the same blueprint used for a brand-new form, so there's something
    to pick from on first run; after that it's entirely admin-owned."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM field_keys")
    (count,) = cur.fetchone()
    if count == 0:
        for key, en, ar, ftype, options, _required in DEFAULT_FIELDS:
            cur.execute(
                """INSERT INTO field_keys (field_key, label_en, label_ar, field_type, options_json)
                   VALUES (%s,%s,%s,%s,%s)""",
                (key, en, ar, ftype, options))
    cur.close()


def _seed_default_form_if_empty(conn, default_port):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM forms")
    (count,) = cur.fetchone()
    if count == 0:
        cur.execute(
            "INSERT INTO forms (id, name, slug, port, active) VALUES (1,%s,%s,%s,1)",
            ("Fan Feedback", "main", default_port))
        _seed_fields(conn, 1, cur=cur)
    cur.close()


def test_connection(db_cfg):
    """Try connecting with the given settings without touching the live pool. Raises on failure."""
    conn = mysql.connector.connect(
        host=db_cfg["host"], port=int(db_cfg.get("port") or 3306),
        user=db_cfg["user"], password=db_cfg["password"], database=db_cfg["database"],
        connection_timeout=10, charset="utf8mb4")
    conn.close()


def connect_and_prepare(db_cfg, default_port=8081):
    """Connect, create/migrate the schema, seed a first form if none exist. Returns a ready pool."""
    pool = _make_pool(db_cfg)
    conn = pool.get_connection()
    try:
        _create_schema(conn)
        _migrate_schema(conn)
        _seed_field_key_library_if_empty(conn)
        _seed_default_form_if_empty(conn, default_port)
        conn.commit()
    finally:
        conn.close()
    return pool


def set_pool(pool):
    global _pool, _last_error
    with _pool_lock:
        _pool = pool
        _last_error = None


def clear_pool(error=None):
    global _pool, _last_error
    with _pool_lock:
        _pool = None
        _last_error = error


def is_connected():
    return _pool is not None


def last_error():
    return _last_error


def _conn():
    if _pool is None:
        raise DBError("database is not connected")
    return _pool.get_connection()


def ping():
    """Cheapest possible round-trip, for the background health-check loop
    (see server.py) that decides whether to fire a db_disconnected alert —
    is_connected() alone only says a pool object exists, not that the
    server behind it is actually still reachable."""
    if _pool is None:
        return False
    try:
        conn = _pool.get_connection()
        try:
            conn.ping(reconnect=False, attempts=1, delay=0)
            return True
        finally:
            conn.close()
    except mysql.connector.Error:
        return False


# ------------------------------------------------------------------ forms

def list_forms(active_only=False):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        sql = "SELECT * FROM forms"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id"
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["active"] = bool(r["active"])
            r["lang_en"] = bool(r["lang_en"]); r["lang_ar"] = bool(r["lang_ar"])
            r["enabled_extra_langs"] = json.loads(r["enabled_extra_langs_json"]) if r["enabled_extra_langs_json"] else []
            del r["enabled_extra_langs_json"]
        return rows
    finally:
        conn.close()


def get_form(form_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM forms WHERE id=%s", (form_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row["active"] = bool(row["active"])
            row["lang_en"] = bool(row["lang_en"]); row["lang_ar"] = bool(row["lang_ar"])
            row["enabled_extra_langs"] = json.loads(row["enabled_extra_langs_json"]) if row["enabled_extra_langs_json"] else []
            del row["enabled_extra_langs_json"]
        return row
    finally:
        conn.close()


def used_ports(exclude_form_id=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        if exclude_form_id:
            cur.execute("SELECT port FROM forms WHERE id != %s", (exclude_form_id,))
        else:
            cur.execute("SELECT port FROM forms")
        ports = {r[0] for r in cur.fetchall()}
        cur.close()
        return ports
    finally:
        conn.close()


def create_form(name, slug, port, lang_en=True, lang_ar=True, enabled_extra_langs=None, expiry_date=None):
    """Creates the form with no questions yet — a new form starts blank so
    an admin picks exactly the field keys it needs from the Field keys
    library, rather than inheriting the full default set."""
    conn = _conn()
    try:
        cur = conn.cursor()
        extra = json.dumps(enabled_extra_langs) if enabled_extra_langs else None
        cur.execute(
            "INSERT INTO forms (name, slug, port, active, lang_en, lang_ar, enabled_extra_langs_json, expiry_date) "
            "VALUES (%s,%s,%s,1,%s,%s,%s,%s)",
            (name, slug, port, 1 if lang_en else 0, 1 if lang_ar else 0, extra, expiry_date))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()


def update_form(form_id, name=None, port=None, lang_en=None, lang_ar=None, enabled_extra_langs=None,
                 expiry_date=_UNSET):
    conn = _conn()
    try:
        cur = conn.cursor()
        sets, params = [], []
        if name is not None:
            sets.append("name=%s"); params.append(name)
        if port is not None:
            sets.append("port=%s"); params.append(port)
        if lang_en is not None:
            sets.append("lang_en=%s"); params.append(1 if lang_en else 0)
        if lang_ar is not None:
            sets.append("lang_ar=%s"); params.append(1 if lang_ar else 0)
        if enabled_extra_langs is not None:
            sets.append("enabled_extra_langs_json=%s"); params.append(json.dumps(enabled_extra_langs) if enabled_extra_langs else None)
        if expiry_date is not _UNSET:
            sets.append("expiry_date=%s"); params.append(expiry_date)   # None clears it
        if sets:
            params.append(form_id)
            cur.execute("UPDATE forms SET " + ", ".join(sets) + " WHERE id=%s", params)
            conn.commit()
        cur.close()
    finally:
        conn.close()


def set_form_active(form_id, active):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE forms SET active=%s WHERE id=%s", (1 if active else 0, form_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def update_branding(form_id, org_name=None, primary_color=None, ink_color=None,
                     subtitle_en=None, subtitle_ar=None, logo_filename=None, clear_logo=False):
    conn = _conn()
    try:
        cur = conn.cursor()
        sets, params = [], []
        if org_name is not None:
            sets.append("org_name=%s"); params.append(org_name or None)
        if primary_color is not None:
            sets.append("primary_color=%s"); params.append(primary_color or None)
        if ink_color is not None:
            sets.append("ink_color=%s"); params.append(ink_color or None)
        if subtitle_en is not None:
            sets.append("subtitle_en=%s"); params.append(subtitle_en or None)
        if subtitle_ar is not None:
            sets.append("subtitle_ar=%s"); params.append(subtitle_ar or None)
        if clear_logo:
            sets.append("logo_filename=NULL")
        elif logo_filename is not None:
            sets.append("logo_filename=%s"); params.append(logo_filename)
        if sets:
            params.append(form_id)
            cur.execute("UPDATE forms SET " + ", ".join(sets) + " WHERE id=%s", params)
            conn.commit()
        cur.close()
    finally:
        conn.close()


# ------------------------------------------------------------- form fields

def list_fields(form_id, active_only=False):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        sql = "SELECT * FROM form_fields WHERE form_id = %s"
        params = [form_id]
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY sort_order, id"
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["options"] = json.loads(r["options_json"]) if r["options_json"] else None
            r["labels_extra"] = json.loads(r["labels_extra_json"]) if r["labels_extra_json"] else {}
            r["required"] = bool(r["required"])
            r["active"] = bool(r["active"])
            del r["labels_extra_json"]
        return rows
    finally:
        conn.close()


def list_all_fields():
    """Every field, across every form — for export label lookups."""
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM form_fields ORDER BY form_id, sort_order, id")
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["options"] = json.loads(r["options_json"]) if r["options_json"] else None
            r["labels_extra"] = json.loads(r["labels_extra_json"]) if r["labels_extra_json"] else {}
            r["required"] = bool(r["required"])
            r["active"] = bool(r["active"])
            del r["labels_extra_json"]
        return rows
    finally:
        conn.close()


def create_field(form_id, data):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM form_fields WHERE form_id=%s", (form_id,))
        (next_order,) = cur.fetchone()
        cur.execute(
            """INSERT INTO form_fields (form_id, field_key, label_en, label_ar, labels_extra_json, field_type,
                   options_json, required, sort_order, active)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
            (form_id, data["field_key"], data["label_en"], data["label_ar"],
             json.dumps(data["labels_extra"]) if data.get("labels_extra") else None, data["field_type"],
             json.dumps(data["options"]) if data.get("options") else None,
             1 if data.get("required") else 0, next_order))
        conn.commit()
        new_id = cur.lastrowid
        cur.close()
        return new_id
    finally:
        conn.close()


def update_field(field_id, data):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE form_fields SET label_en=%s, label_ar=%s, labels_extra_json=%s, field_type=%s,
                   options_json=%s, required=%s WHERE id=%s""",
            (data["label_en"], data["label_ar"],
             json.dumps(data["labels_extra"]) if data.get("labels_extra") else None, data["field_type"],
             json.dumps(data["options"]) if data.get("options") else None,
             1 if data.get("required") else 0, field_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def get_field(field_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM form_fields WHERE id=%s", (field_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row["options"] = json.loads(row["options_json"]) if row["options_json"] else None
            row["labels_extra"] = json.loads(row["labels_extra_json"]) if row["labels_extra_json"] else {}
            row["required"] = bool(row["required"])
            row["active"] = bool(row["active"])
            del row["labels_extra_json"]
        return row
    finally:
        conn.close()


def set_field_active(field_id, active):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE form_fields SET active=%s WHERE id=%s", (1 if active else 0, field_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def reorder_fields(id_order):
    conn = _conn()
    try:
        cur = conn.cursor()
        for i, field_id in enumerate(id_order):
            cur.execute("UPDATE form_fields SET sort_order=%s WHERE id=%s", (i, field_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


# --------------------------------------------------------------- feedback

def make_reference():
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    import secrets as _secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no I/O/0/1
    tail = "".join(_secrets.choice(alphabet) for _ in range(4))
    return "FB-%s-%s" % (day, tail)


def save_feedback(form_id, rec):
    conn = _conn()
    try:
        cur = conn.cursor()
        for _ in range(5):                              # retry on rare collision
            ref = make_reference()
            try:
                cur.execute(
                    """INSERT INTO feedback (form_id, reference, created_at, first_name, last_name,
                           email, consent, language, extra_fields, ip, user_agent)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (form_id, ref, datetime.now(timezone.utc), rec["firstName"], rec["lastName"],
                     rec["email"], 1, rec["language"], json.dumps(rec["extra"]),
                     rec["ip"], rec["userAgent"]))
                conn.commit()
                cur.close()
                return ref
            except mysql.connector.Error as e:
                if e.errno == 1062:                     # duplicate key on `reference`
                    conn.rollback()
                    continue
                raise
        raise DBError("could not allocate a reference number")
    finally:
        conn.close()


def _submission_filter_sql(filters, prefix=""):
    conditions, params = [], []
    if filters.get("form_id"):
        conditions.append(prefix + "form_id = %s"); params.append(filters["form_id"])
    elif filters.get("form_ids") is not None:
        # A role-scoped user with more than one assigned form and no single
        # form_id chosen — restrict to exactly their assigned set. An empty
        # list (assigned to no forms) must still exclude every row, not be
        # treated as "no filter" the way form_id's falsy check above is.
        ids = list(filters["form_ids"])
        if ids:
            conditions.append(prefix + "form_id IN (%s)" % ",".join(["%s"] * len(ids)))
            params += ids
        else:
            conditions.append("1=0")
    if filters.get("date_from"):
        conditions.append(prefix + "created_at >= %s"); params.append(filters["date_from"])
    if filters.get("date_to"):
        conditions.append(prefix + "created_at <= %s"); params.append(filters["date_to"])
    if filters.get("status"):
        conditions.append(prefix + "status = %s"); params.append(filters["status"])
    if filters.get("q"):
        like = "%" + filters["q"] + "%"
        conditions.append("(%(p)sfirst_name LIKE %%s OR %(p)slast_name LIKE %%s OR "
                           "%(p)semail LIKE %%s OR %(p)sreference LIKE %%s)" % {"p": prefix})
        params += [like, like, like, like]
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params


def list_submissions(filters=None, page=1, page_size=25):
    filters = filters or {}
    where, params = _submission_filter_sql(filters, prefix="f.")
    page = max(1, page)
    offset = (page - 1) * page_size

    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM feedback f " + where, params)
        total = cur.fetchone()["n"]
        cur.execute(
            "SELECT f.id, f.form_id, ff.name AS form_name, f.reference, f.created_at, "
            "f.first_name, f.last_name, f.email, f.language, f.status "
            "FROM feedback f LEFT JOIN forms ff ON ff.id = f.form_id " + where +
            " ORDER BY f.id DESC LIMIT %s OFFSET %s", params + [page_size, offset])
        rows = cur.fetchall()
        cur.close()
        return rows, total
    finally:
        conn.close()


def export_rows(filters=None):
    """Everything collected (optionally filtered), with extra_fields flattened
    using each row's own form field order/labels. Retired fields' data still
    comes back, just appended after."""
    filters = filters or {}
    all_fields = list_all_fields()
    label_of = {}
    order_of = {}
    for f in all_fields:
        label_of[(f["form_id"], f["field_key"])] = f["label_en"]
        order_of.setdefault(f["form_id"], []).append(f["field_key"])
    form_names = {f["id"]: f["name"] for f in list_forms(active_only=False)}

    where, params = _submission_filter_sql(filters)
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT form_id, reference, created_at, first_name, last_name, email, "
            "extra_fields, language, status FROM feedback " + where + " ORDER BY id DESC", params)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    seen_extra_keys = []
    parsed = []
    for r in rows:
        extra = json.loads(r["extra_fields"]) if r["extra_fields"] else {}
        for k in extra:
            if k not in seen_extra_keys:
                seen_extra_keys.append(k)
        parsed.append((r, extra))

    out = []
    for r, extra in parsed:
        row = {
            "Form": form_names.get(r["form_id"], "—"),
            "Reference": r["reference"], "Received (UTC)": r["created_at"],
            "First name": r["first_name"], "Last name": r["last_name"], "Email": r["email"],
        }
        for k in seen_extra_keys:
            label = label_of.get((r["form_id"], k), k)
            row[label] = extra.get(k, "")
        row["Language"] = r["language"]
        row["Status"] = r["status"]
        out.append(row)
    return out


# -------------------------------------------------------- roles & users
#
# The one bootstrap admin account (config.json, see config_store.py) still
# exists outside of all this and is untouched — it's the only thing that
# has to be reachable before a database connection is. Everything below is
# for *additional* accounts an admin can hand out once the database is up,
# each restricted to a custom role's permission checklist and (unless the
# role applies to every form) a specific set of forms. See
# docs/ROLES_AND_PERMISSIONS.md for the general pattern this implements.

def list_roles():
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM admin_roles ORDER BY name")
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["permissions"] = json.loads(r["permissions_json"])
            r["applies_to_all_forms"] = bool(r["applies_to_all_forms"])
            del r["permissions_json"]
        return rows
    finally:
        conn.close()


def get_role(role_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM admin_roles WHERE id=%s", (role_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row["permissions"] = json.loads(row["permissions_json"])
            row["applies_to_all_forms"] = bool(row["applies_to_all_forms"])
            del row["permissions_json"]
        return row
    finally:
        conn.close()


def create_role(name, permissions, applies_to_all_forms):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO admin_roles (name, permissions_json, applies_to_all_forms) VALUES (%s,%s,%s)",
            (name, json.dumps(permissions), 1 if applies_to_all_forms else 0))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()


def update_role(role_id, name, permissions, applies_to_all_forms):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE admin_roles SET name=%s, permissions_json=%s, applies_to_all_forms=%s WHERE id=%s",
            (name, json.dumps(permissions), 1 if applies_to_all_forms else 0, role_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_role(role_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM admin_roles WHERE id=%s", (role_id,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def role_in_use(role_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM admin_users WHERE role_id=%s", (role_id,))
        (count,) = cur.fetchone()
        cur.close()
        return count > 0
    finally:
        conn.close()


def _attach_user_forms(cur, users):
    if not users:
        return
    ids = [u["id"] for u in users]
    placeholders = ",".join(["%s"] * len(ids))
    cur.execute("SELECT user_id, form_id FROM admin_user_forms WHERE user_id IN (%s)" % placeholders, ids)
    by_user = {}
    for row in cur.fetchall():
        by_user.setdefault(row["user_id"], []).append(row["form_id"])
    for u in users:
        u["form_ids"] = by_user.get(u["id"], [])


def list_admin_users():
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT admin_users.*, admin_roles.name AS role_name, "
            "admin_roles.applies_to_all_forms AS role_applies_to_all_forms "
            "FROM admin_users JOIN admin_roles ON admin_roles.id = admin_users.role_id "
            "ORDER BY admin_users.username")
        rows = cur.fetchall()
        for r in rows:
            r["active"] = bool(r["active"])
            r["role_applies_to_all_forms"] = bool(r["role_applies_to_all_forms"])
            del r["password_hash"]; del r["salt"]
        _attach_user_forms(cur, rows)
        cur.close()
        return rows
    finally:
        conn.close()


def get_admin_user_by_username(username):
    """Includes password_hash/salt — for login verification only."""
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT admin_users.*, admin_roles.permissions_json, "
            "admin_roles.applies_to_all_forms AS role_applies_to_all_forms "
            "FROM admin_users JOIN admin_roles ON admin_roles.id = admin_users.role_id "
            "WHERE admin_users.username=%s AND admin_users.active=1", (username,))
        row = cur.fetchone()
        if row:
            row["active"] = bool(row["active"])
            row["role_applies_to_all_forms"] = bool(row["role_applies_to_all_forms"])
            row["permissions"] = json.loads(row["permissions_json"])
            del row["permissions_json"]
            _attach_user_forms(cur, [row])
        cur.close()
        return row
    finally:
        conn.close()


def get_admin_user(user_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT admin_users.*, admin_roles.name AS role_name, "
            "admin_roles.applies_to_all_forms AS role_applies_to_all_forms "
            "FROM admin_users JOIN admin_roles ON admin_roles.id = admin_users.role_id "
            "WHERE admin_users.id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            row["active"] = bool(row["active"])
            row["role_applies_to_all_forms"] = bool(row["role_applies_to_all_forms"])
            del row["password_hash"]; del row["salt"]
            _attach_user_forms(cur, [row])
        cur.close()
        return row
    finally:
        conn.close()


def create_admin_user(username, password_hash, salt, role_id, form_ids):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO admin_users (username, password_hash, salt, role_id) VALUES (%s,%s,%s,%s)",
            (username, password_hash, salt, role_id))
        new_id = cur.lastrowid
        for form_id in (form_ids or []):
            cur.execute("INSERT INTO admin_user_forms (user_id, form_id) VALUES (%s,%s)", (new_id, form_id))
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()


def update_admin_user(user_id, role_id=None, active=None, form_ids=None,
                       password_hash=None, salt=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        sets, params = [], []
        if role_id is not None:
            sets.append("role_id=%s"); params.append(role_id)
        if active is not None:
            sets.append("active=%s"); params.append(1 if active else 0)
        if password_hash is not None:
            sets.append("password_hash=%s"); params.append(password_hash)
        if salt is not None:
            sets.append("salt=%s"); params.append(salt)
        if sets:
            params.append(user_id)
            cur.execute("UPDATE admin_users SET " + ", ".join(sets) + " WHERE id=%s", params)
        if form_ids is not None:
            cur.execute("DELETE FROM admin_user_forms WHERE user_id=%s", (user_id,))
            for form_id in form_ids:
                cur.execute("INSERT INTO admin_user_forms (user_id, form_id) VALUES (%s,%s)", (user_id, form_id))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_admin_user(user_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM admin_user_forms WHERE user_id=%s", (user_id,))
        cur.execute("DELETE FROM admin_users WHERE id=%s", (user_id,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


# ------------------------------------------------------------- alerts
#
# scope='global' rows (form_id NULL) are for db_disconnected — the whole
# app losing its database, not any one form's business. Every other
# trigger type is scope='form', tied to one form_id. See notifier.py for
# what actually fires these.

def list_alert_rules(form_id=None, trigger_type=None, enabled_only=False):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        conditions, params = [], []
        if form_id is not None:
            conditions.append("form_id=%s"); params.append(form_id)
        if trigger_type is not None:
            conditions.append("trigger_type=%s"); params.append(trigger_type)
        if enabled_only:
            conditions.append("enabled=1")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute("SELECT * FROM alert_rules " + where + " ORDER BY id", params)
        rows = cur.fetchall()
        cur.close()
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows
    finally:
        conn.close()


def create_alert_rule(form_id, trigger_type, channel, destination, enabled=True):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO alert_rules (form_id, trigger_type, channel, destination, enabled) "
            "VALUES (%s,%s,%s,%s,%s)",
            (form_id, trigger_type, channel, destination, 1 if enabled else 0))
        new_id = cur.lastrowid
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()


def update_alert_rule(rule_id, channel=None, destination=None, enabled=None):
    conn = _conn()
    try:
        cur = conn.cursor()
        sets, params = [], []
        if channel is not None:
            sets.append("channel=%s"); params.append(channel)
        if destination is not None:
            sets.append("destination=%s"); params.append(destination)
        if enabled is not None:
            sets.append("enabled=%s"); params.append(1 if enabled else 0)
        if sets:
            params.append(rule_id)
            cur.execute("UPDATE alert_rules SET " + ", ".join(sets) + " WHERE id=%s", params)
            conn.commit()
        cur.close()
    finally:
        conn.close()


def delete_alert_rule(rule_id):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM alert_rules WHERE id=%s", (rule_id,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def get_alert_rule(rule_id):
    conn = _conn()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM alert_rules WHERE id=%s", (rule_id,))
        row = cur.fetchone()
        cur.close()
        if row:
            row["enabled"] = bool(row["enabled"])
        return row
    finally:
        conn.close()
