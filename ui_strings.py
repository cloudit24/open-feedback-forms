"""
The public feedback page's fixed UI chrome text (button labels, error
messages, the Terms and Conditions copy...) — kept here, in English, as the
single source text used when auto-translating a newly added language via
`translate_client`. English and Arabic themselves are NOT generated from
this file; they stay hand-written as `T.en`/`T.ar` in `public/index.html`
exactly as before, so adding this file changes nothing for either of them.
"""

UI_STRINGS = {
    "club": "Your Club", "fanvoice": "Fan Voice",
    "title": "Tell us what you think",
    "subtitle": "Your feedback helps us improve matchdays, facilities and everything around the club. It takes about two minutes.",
    "firstName": "First name", "lastName": "Last name", "email": "Email address",
    "optional": "(optional)", "choose": "Please choose...",
    "setupTitle": "This form isn't ready yet",
    "setupBody": "The club is still setting this up. Please check back shortly.",
    "privacyTitle": "Terms and Conditions",
    "privacyBody": "We're grateful for the time you're taking to share your thoughts with us. {club} collects your name, email address, mobile number and nationality so our team can review your feedback, keep you updated, and look after our ongoing relationship with you as one of our fans. We may also contact you about the feedback you share, whether to follow up, ask a clarifying question, or simply say thank you. We treat your details with care, we never sell them, and we never share them outside the club. You're always welcome to ask us to update or delete your information by emailing the club.",
    "consent": "I agree to terms and conditions",
    "submit": "Send feedback", "sending": "Sending...",
    "errFirst": "Please enter your first name.",
    "errLast": "Please enter your last name.",
    "errEmail": "Please enter a valid email address.",
    "errRequired": "This is required.",
    "errPhoneFormat": "Please enter a valid mobile number, digits only.",
    "errEmailFormat": "Please enter a valid email address.",
    "errShort": "Please write at least 10 characters.",
    "errSelect": "Please make a choice.",
    "errRating": "Please give a rating.",
    "errConsent": "You need to agree before we can accept your feedback.",
    "errFix": "Please check the highlighted fields and try again.",
    "errNet": "We could not send your feedback. Please check your connection and try again.",
    "errRate": "You have sent several messages already. Please try again later.",
    "errBot": "Please complete the verification check and try again.",
    "doneTitle": "Thank you — we've got it",
    "doneBody": "Your feedback has reached the club. If you asked us to contact you, someone will be in touch.",
    "refLabel": "YOUR REFERENCE", "again": "Send more feedback",
}

# The "{club}" placeholder is substituted at render time, not translated —
# every language uses this same literal template.
UI_TEMPLATE_ONLY = {"foot": "{club}"}

# Star-rating words, 1..5 (index 0 is the unrated placeholder and stays "").
UI_WORDS = ["", "Very poor", "Poor", "Okay", "Good", "Excellent"]
