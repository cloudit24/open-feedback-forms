# Sonnet prompts: professional upgrade (1–7)

Run these one at a time, in order, each in a new Sonnet session opened in
`C:\Users\DFCA\Music\open-feedback-forms`. Paste the **Common rules** block
first, then one prompt. Don't start the next one until the last one is
tested and released.

---

## Common rules (paste before every prompt)

```
You are working on Open Feedback Forms (repo cloudit24/open-feedback-forms,
folder C:\Users\DFCA\Music\open-feedback-forms). It is a GENERAL-PURPOSE,
self-hosted, bilingual (English + Arabic, full right-to-left) feedback form
platform. It is NOT a football/club app - never add club, fan, match or
stadium wording anywhere.

Stack (keep it):
- Python standard library only (ThreadingHTTPServer) in server.py, plus
  mysql-connector for MariaDB. db.py holds all SQL. config_store.py holds
  config.json settings. ui_strings.py holds public-form text (EN/AR).
- public/index.html = the public form (one self-contained file: HTML+CSS+JS).
- admin/index.html = the admin panel (one self-contained file).
- Docker: docker-compose.yml, Dockerfile, install.sh. Admin on port 8800.

Hard rules:
1. Build everything ourselves. No new libraries, frameworks, CDNs or npm.
   The license is MIT - do NOT copy code from Formbricks, HeyForm, OpnForm,
   Typebot or LimeSurvey (AGPL/GPL). Ideas only.
2. Every new text a user sees must exist in English AND Arabic, and every
   layout must work in RTL. Check both.
3. Database changes: add columns only, with a matching entry in MIGRATE_SQL
   in db.py, with safe defaults so old forms keep working unchanged. Never
   drop or rename columns. Never delete user data.
4. Old forms must look and behave exactly as before unless the admin turns
   the new option on.
5. Back up first: git commit the clean state before you start.
6. Test before saying done:
   - start a throwaway MariaDB:  docker run -d --name off-test-db -p 13306:3306
     -e MARIADB_ROOT_PASSWORD=test -e MARIADB_DATABASE=off mariadb:11
   - run server.py locally with OFF_CONFIG_DIR pointing to a temp folder and
     the DB on 127.0.0.1:13306
   - open the admin and a public form in the browser, test EN and AR, light
     and dark, phone width (375px), and check the console has no errors
   - submit a real test response and confirm it is saved
   - remove the test container and temp folder afterwards
7. Finish: bump VERSION (minor version, e.g. 1.7.0), add a CHANGELOG.md
   entry in plain English, update hub.json "status" and "next" in one short
   line each. Commit. Do NOT push or tag - ask the user first.
8. Report back in plain, short English: what changed, what you tested, what
   did not work. No jargon. The user is not a developer-writer; keep it brief.
9. Do only what this prompt asks. Extra ideas go in one line at the end.
```

---

## Prompt 1: Remove club wording + add templates

```
Goal: make the app neutral and professional. A new user should never see
anything about clubs, fans, matches, matchdays or stadiums.

1. Search the whole repo (server.py, db.py, ui_strings.py, notifier.py,
   reset_password.py, public/index.html, admin/index.html, README.md,
   install.sh) for: club, fan, match, matchday, stadium, "Your Club",
   "Fan Feedback". There are about 80 hits. Replace each with neutral
   wording (e.g. "Your organisation", "Feedback", "our updates"), in EN and AR.
   Examples: the marketing-consent text should say "Yes, keep me updated
   with news and offers." The privacy text must use the form's
   organisation name, not "the club".
2. Built-in catalogue fields that only make sense for football
   (attendance_frequency, heard_about "today's match", exp_entry
   "Stadium entry", etc.) must NOT be deleted - existing forms use them.
   Instead: reword the generic ones (heard_about -> "How did you hear about
   us?"), and move the venue-specific ones into a template (step 3) rather
   than the default new form.
3. Templates. When an admin clicks "New form", show a picker first:
   - Blank form
   - Customer satisfaction (CSAT)
   - Net Promoter Score (NPS)
   - Event feedback
   - Product feedback
   - Employee feedback
   - Website feedback
   - Venue / visit experience (holds the old stadium-style questions,
     worded neutrally: entry & access, seating, cleanliness, food,
     atmosphere, staff)
   Each template = a list of field keys + its own default title and
   description in EN and AR. Store templates as a Python list in a new
   file templates.py (data only). Creating from a template uses the same
   code path as cloning. Blank form = no questions at all.
4. The default form created by a fresh install must be "Customer
   satisfaction", named "Feedback", not anything club-related.
5. Existing installs: do NOT rename or change anyone's existing forms,
   fields or submissions. Only defaults and new forms change.

Test: fresh install shows no club words (grep the served HTML of admin and
a new form for club|fan|match|stadium - must be 0). Create one form from
each template in EN and AR. An old form from before the update still shows
its original questions unchanged.
```

---

## Prompt 2: Form title + text styling (bold, italic, font, size)

```
Goal: each form gets a proper title and description, with basic text
styling, set by the admin under Edit -> Branding, separately for EN and AR.

1. New per-form settings (add columns to forms, with MIGRATE_SQL):
   - title_en, title_ar (short text)
   - description_en, description_ar (styled text, see 2). The existing
     subtitle fields become the description - migrate their values in, do
     not lose them.
   - font (one of a fixed list, default 'system')
   - text_size ('small' | 'normal' | 'large', default 'normal')
   - title_align ('start' | 'center', default 'start')
2. Our own tiny text editor (no library). A contenteditable box with a
   toolbar: Bold, Italic, Underline, Link, Bulleted list, Clear formatting.
   Keyboard: Ctrl+B / Ctrl+I / Ctrl+U. It must work in RTL for Arabic.
   SECURITY: the server must sanitise the saved HTML with an allow-list:
   only <b> <strong> <i> <em> <u> <a href="http(s)://..."> <ul> <ol> <li>
   <br> <p>. Strip every other tag and every attribute except href on <a>.
   Add rel="noopener" target="_blank" to links. Write this sanitiser in
   Python using html.parser from the standard library, with unit tests
   in tests/test_sanitise.py covering script tags, onerror attributes,
   javascript: links and nested junk.
   Use the same editor for the question labels' help text if simple; if
   not simple, skip and say so.
3. Fonts: offer about 6 that support both Latin and Arabic: System default,
   Inter + Noto Sans Arabic, Cairo, Tajawal, IBM Plex Sans Arabic,
   Noto Kufi Arabic. Self-host the font files in public/fonts/ (download
   the .woff2 files from Google Fonts once, commit them, check their OFL
   license and keep the license file next to them). No runtime CDN calls.
4. Text size presets scale the whole public form (title, questions,
   answers) using a CSS variable. No free pixel typing.
5. Admin Branding panel: a live preview on the right that updates as you
   type (title, description, font, size), with an EN/AR switch.
6. Public form: show the title as the main heading (h1) and the
   description under it. If the title is empty, fall back to the form
   name. Browser tab title = the form title only (no "— Feedback").

Test: XSS attempt (<img src=x onerror=alert(1)>, <script>, javascript:
link) is stripped on save. Every font renders English and Arabic. All three
sizes look right on phone and desktop. Old forms show their old subtitle
as the description.
```

---

## Prompt 3: Rating questions as tap buttons

```
Goal: no more "Please choose..." dropdowns for ratings. Ratings must be
tapped, like modern form apps.

1. New question display styles (keep the stored answers compatible):
   - Stars (1–5)
   - Faces (5 faces, very bad -> very good, drawn with inline SVG, no
     emoji fonts)
   - Number scale 0–10 (NPS): a row of 11 buttons with "Not likely" /
     "Extremely likely" labels under the ends, EN and AR
   - Number scale 1–5 or 1–10
   - Choice buttons: for 'select' questions with up to 6 options, show
     the options as big tappable buttons instead of a dropdown (admin can
     switch back to dropdown)
   - Matrix/grid: several questions sharing the same scale (e.g. the six
     "Excellent..Very poor" experience questions) shown as one table, with
     rows = questions and columns = scale. On phone width the grid turns
     into stacked cards.
2. Add a display_style column to form_fields (MIGRATE_SQL, default NULL =
   current behaviour). Admin chooses the style per question in the
   question editor, with a small preview.
3. Accessibility: every button group is a proper radio group (arrow keys
   move, Space/Enter selects, visible focus ring, aria-labels in the
   current language). Works in RTL (scale direction follows the language).
4. Required ratings must show the same error message style as other
   fields.
5. The existing 'nps' and 'overall_satisfaction' fields in new templates
   should default to the 0–10 scale and Stars.

Test: keyboard-only completion of a form. Screen-reader labels present.
Submitted values are identical in format to the old dropdown values (check
the CSV export is unchanged in shape). Phone width: grid turns into cards.
```

---

## Prompt 4: Layout choice: one page, steps, or one question at a time

```
Goal: forms stop being one long wall. The admin picks the layout per form.

1. New forms column layout_mode: 'single_page' (current, default for old
   forms) | 'steps' | 'one_at_a_time' (default for new forms).
2. Steps: questions are grouped by their category (or by "page break"
   items the admin can add between questions). Next / Back buttons, each
   step validated before moving on.
3. One at a time: one question fills the screen, smooth slide to the next,
   Enter = next, number/letter keys pick an option, Back button and
   Up-arrow go back. Auto-advance after a tap on single-choice and rating
   questions (short delay so the user sees their choice).
4. Progress bar at the top for steps and one-at-a-time ("3 of 12"), EN/AR,
   RTL-aware.
5. Optional welcome screen (title, description, "Start" button) and a
   proper thank-you screen (title, message, optional button with a link),
   both editable per form in EN and AR, using the editor from prompt 2.
6. Answers are kept if the user goes back. If the page reloads mid-form,
   restore answers from sessionStorage (wrapped in try/catch).
7. Submission still posts once at the end to the same api/feedback
   endpoint - the server side must not change shape.

Test: all three layouts in EN and AR, phone and desktop, keyboard only.
Required-field errors show on the right step. Reload mid-form keeps
answers. Old forms still show single_page exactly as before.
```

---

## Prompt 5: Conditional questions + hidden fields from the link

```
Goal: show a question only when an earlier answer matches, and record
extra info from the form link.

1. Conditional logic per question: "Show this question only if
   [earlier question] [is / is not / is one of / is at least / is at most]
   [value]". Support AND of several conditions; OR can wait. Store as JSON
   in a new form_fields column show_if (MIGRATE_SQL, NULL = always shown).
   Example: if NPS <= 6, show "What should we fix?".
2. Admin: a simple rule builder in the question editor (dropdowns, no
   code). Warn if a rule points at a question that comes later or was
   deleted.
3. Public form: hidden questions are not shown, not required, and not
   submitted. The SERVER must re-check the rules on submit (never trust
   the browser): a required question that is hidden by its rule is not
   required; an answer to a hidden question is dropped.
4. Hidden fields: the admin lists allowed names (e.g. branch, source,
   staff). Values come from the link: /f/<slug>/?branch=dubai. Only listed
   names are saved, max 100 chars each, plain text. They appear as extra
   columns in submissions view and CSV export.
5. Works with all three layouts from prompt 4 (steps with no visible
   questions are skipped; progress count only counts visible questions).

Test: unit tests for the server-side rule check in tests/test_logic.py.
Browser: a rule hides/shows live in EN and AR. Tampering (posting an
answer to a hidden question) is dropped. Hidden field values land in CSV.
```

---

## Prompt 6: Results dashboard with charts + NPS/CSAT scores

```
Goal: the admin sees results at a glance, not just a table.

1. New "Summary" tab per form in the admin panel, before "Submissions".
2. Top cards: total responses, responses this week, completion date range,
   NPS score (if the form has a 0–10 question: % promoters 9–10 minus %
   detractors 0–6, shown -100..+100 with promoters/passives/detractors
   bar), CSAT (% of 4–5 on a 1–5 or star question), average rating per
   rating question.
3. Per question:
   - choice / rating: horizontal bar chart with counts and %
   - 0–10: bar per number, coloured red/grey/green
   - text answers: latest 10, with "show all"
   - matrix: a coloured table (heatmap)
4. Responses over time: a simple line or bar chart per day/week.
5. Filter by date range and by hidden field (from prompt 5) - everything
   on the page updates.
6. Charts drawn with our own small SVG code in admin/index.html. No chart
   library. Must look good in light and dark admin theme and in RTL.
7. Do the counting in SQL in db.py (GROUP BY), not by loading every row
   into Python. One new endpoint GET /admin/forms/<id>/summary?from=&to=
   that returns JSON. Respect existing user permissions
   (docs/ROLES_AND_PERMISSIONS.md).
8. "Download PDF" is out of scope; "Print" with a clean print stylesheet
   is enough.

Test: seed 500 fake responses into the test DB with a script in
scripts/seed_test_data.py, check every number by hand against a SQL
query, check page loads in under 1 second, check a viewer-role user sees
only what they're allowed to.
```

---

## Prompt 7: Webhooks + close after date or N responses

```
Goal: send each new response to another system, and close forms
automatically.

1. Webhooks per form: admin adds one or more URLs (https only, except
   localhost for testing). On each new submission, POST JSON:
   { form: {id, name, slug}, submission: {id, created_at, answers: {...},
   hidden: {...}} }. Signed with a per-form secret: header
   X-OFF-Signature = HMAC-SHA256 of the body (show the secret once in the
   admin with a "copy" button and a "regenerate" button).
2. Sending must never slow down or break the form submission: send in a
   background thread with a 5-second timeout, retry 3 times (after 1 min,
   5 min, 30 min). Keep a log of the last 50 attempts per webhook
   (status code, time, error) visible in the admin, with a "Send test"
   button.
3. Block requests to private/internal IP ranges unless the admin ticks
   "Allow local network" (protects against SSRF). Resolve the host and
   check the IP before sending.
4. Close rules per form (the expiry date already exists - keep it):
   - close after N responses (new column max_responses, NULL = no limit)
   - custom "form closed" message in EN and AR (editor from prompt 2)
   - optional opening date (form shows "Opens on ..." before then)
   The server enforces these on submit, not only the page.
5. Admin form list shows a clear status badge: Open / Scheduled /
   Closed (date) / Closed (limit reached).

Test: a local test receiver (python -m http.server-style script in
scripts/webhook_receiver.py) gets the JSON and the signature verifies.
Receiver down -> submission still succeeds and retries are logged.
Private IP is blocked by default. Response limit closes the form exactly at
N under quick double submissions (use a DB-level check, not just Python).
```
