"""
Starting points offered when an admin clicks "Add a form". Data only.

Each template is a list of question keys (the keys come from the blueprint
in db.py: DEFAULT_FIELDS and LIBRARY_ONLY_FIELDS) plus a default title and
description in English and Arabic. "blank" has no questions at all.

Using a template creates the form first, then copies each listed question
onto it in order, the same way cloning copies questions.
"""

TEMPLATES = [
    {
        "key": "blank",
        "name_en": "Blank form", "name_ar": "نموذج فارغ",
        "title_en": "Feedback", "title_ar": "ملاحظات",
        "description_en": "Start with no questions and add your own.",
        "description_ar": "ابدأ بدون أي أسئلة وأضف أسئلتك الخاصة.",
        "fields": [],
    },
    {
        "key": "csat",
        "name_en": "Customer satisfaction (CSAT)", "name_ar": "رضا العملاء",
        "title_en": "Feedback", "title_ar": "ملاحظات",
        "description_en": "Tell us how we did. It only takes a minute.",
        "description_ar": "أخبرنا كيف كانت تجربتك. لن يستغرق الأمر سوى دقيقة.",
        "fields": ["overall_satisfaction", "exp_staff", "nps", "feedback_message", "marketing_optin"],
    },
    {
        "key": "nps",
        "name_en": "Net Promoter Score (NPS)", "name_ar": "مؤشر صافي الترويج",
        "title_en": "How are we doing?", "title_ar": "كيف نبلي؟",
        "description_en": "One quick question about whether you would recommend us.",
        "description_ar": "سؤال سريع عن مدى احتمال أن توصي بنا.",
        "fields": ["nps", "nps_reason", "marketing_optin"],
    },
    {
        "key": "event",
        "name_en": "Event feedback", "name_ar": "ملاحظات عن فعالية",
        "title_en": "Event feedback", "title_ar": "ملاحظات عن الفعالية",
        "description_en": "Thanks for joining us. Tell us what you thought of the event.",
        "description_ar": "شكرًا لحضورك. أخبرنا برأيك في الفعالية.",
        "fields": ["event_overall", "event_content", "event_organisation", "event_venue",
                   "heard_about", "nps", "feedback_message"],
    },
    {
        "key": "product",
        "name_en": "Product feedback", "name_ar": "ملاحظات عن منتج",
        "title_en": "Product feedback", "title_ar": "ملاحظات عن المنتج",
        "description_en": "Help us make the product better.",
        "description_ar": "ساعدنا في تحسين المنتج.",
        "fields": ["product_overall", "product_quality", "product_value", "product_ease",
                   "product_missing", "nps"],
    },
    {
        "key": "employee",
        "name_en": "Employee feedback", "name_ar": "ملاحظات الموظفين",
        "title_en": "Employee feedback", "title_ar": "ملاحظات الموظفين",
        "description_en": "Your honest opinion helps us make this a better place to work.",
        "description_ar": "رأيك الصريح يساعدنا على جعل بيئة العمل أفضل.",
        "fields": ["emp_overall", "emp_management", "emp_environment", "emp_growth",
                   "emp_communication", "emp_suggestion"],
    },
    {
        "key": "website",
        "name_en": "Website feedback", "name_ar": "ملاحظات عن الموقع",
        "title_en": "Website feedback", "title_ar": "ملاحظات عن الموقع",
        "description_en": "Tell us about your experience on our website.",
        "description_ar": "أخبرنا عن تجربتك على موقعنا.",
        "fields": ["web_overall", "web_find", "web_design", "web_speed", "web_problem"],
    },
    {
        "key": "venue",
        "name_en": "Venue / visit experience", "name_ar": "تجربة المكان / الزيارة",
        "title_en": "Your visit", "title_ar": "زيارتك",
        "description_en": "Tell us how your visit went.",
        "description_ar": "أخبرنا كيف كانت زيارتك.",
        "fields": ["attendance_frequency", "heard_about", "exp_entry", "exp_seating",
                   "exp_cleanliness", "exp_food", "exp_atmosphere", "exp_staff",
                   "overall_satisfaction", "nps", "feedback_message", "marketing_optin"],
    },
]

# The template a brand-new install's first form is made from.
DEFAULT_TEMPLATE_KEY = "csat"
DEFAULT_FORM_NAME = "Feedback"


def get(key):
    for t in TEMPLATES:
        if t["key"] == key:
            return t
    return None
