# Changelog

What changed in each release, in plain English. The admin panel reads this
file from GitHub to show what a new version fixes.

## 1.13.1

- Terms and Conditions can now be edited per form (Questions & design >
  Branding): title, text and the tick-box wording, in English and Arabic.
  Leave a box blank to keep the standard wording.

## 1.13.0

- Admin: form settings, the question editor and the other editors now open
  inside the app, with the header and tabs still on screen, instead of a bare
  separate page. Form settings are grouped into Basics, Address, Open and
  close, and Webhooks, with shorter help (details fold away) and a Save bar
  that stays at the bottom of the screen.
- Admin: the form buttons "Edit" and "Manage" are renamed "Questions & design"
  and "Settings", so it's clear which one does what.
- Fixed: Submissions showed empty Name and Email for forms that ask First name,
  Last name and Email address as questions, and the CSV had two sets of name
  and email columns. Now there is one Name and one Email, taken from wherever
  the form asked them, and search finds them too.
- New: click any row in Submissions to see the whole response - every question
  and answer, in English or Arabic. A new "Answers" column shows how many
  questions each response answered, so truly empty ones stand out.
- CSV: answers show their wording ("Good") instead of internal keys, the same
  question is one column across forms, and a one-form file follows that form's
  question order. Downloading without picking a form now asks first.
- Admin: "+ Add a form" and "+ Add a field" now sit at the top of their lists,
  next to a new "Field keys" shortcut that jumps straight to the catalogue.
- New: webhooks. A form's Manage panel can send every new response to another
  system. Add a web address (https, or http for localhost) and we POST the answer
  as JSON, signed with a secret so the other side can check it really came from
  here (header X-OFF-Signature, an HMAC-SHA256 of the body). The secret is shown
  once, with a Copy button, and you can make a new one at any time. Only answers
  that were really saved are sent (questions hidden by a rule are not).
- Sending never slows down or breaks the form. If the other side is down we try
  again after 1 minute, 5 minutes and 30 minutes, and each webhook keeps a log of
  its last 50 attempts, plus a "Send test" button. Tries that were waiting are not
  lost if the app restarts.
- Safety: addresses on a private or internal network (for example 10.x.x.x,
  192.168.x.x, localhost) are refused unless you tick "Allow local network".
  Managing webhooks needs the same permission as editing the form.
- New: close rules. A form can close after a number of responses, open on a date
  (before that it says "Opens on ..."), and show your own "closed" and "opens
  soon" messages in English and Arabic. The expiry date works as before.
  The database makes sure two people sending at the same moment cannot both take
  the last place.
- The form list now shows a status: Open, Scheduled, Closed (date) or Closed
  (limit reached). Forms you already have are unchanged and stay open.
- Also added: scripts/webhook_receiver.py, a tiny receiver for trying webhooks out.

## 1.12.0

- New: a "Summary" tab, before Submissions, shows the results of one form at a
  glance. Pick a form (and, if you like, a date range) and see: total responses,
  responses in the last 7 days, the date range, the NPS score, the CSAT score
  and the average of each rating question.
- NPS is worked out from any 0 to 10 question (promoters 9-10 minus detractors
  0-6, from -100 to +100, with a red / grey / green bar). CSAT is the share of
  4s and 5s on a star, face or 1 to 5 question.
- Every question gets its own chart: bars with counts and percentages for
  choices and ratings, one red / grey / green bar per number for 0 to 10
  questions, a coloured table for grid questions (darker = bigger share of the
  row), and the latest 10 written answers with "Show all". Each question says how
  many people answered it, so questions hidden by a rule are not counted against
  everyone. A chart shows responses per day (per week for long ranges).
- Filter by date range and by a link field (for example branch = dubai) and the
  whole page updates. "Print" gives a clean printout.
- Only people allowed to see the dashboard for that form can open it. Written
  answers also need permission to view submissions.
- No new libraries: the charts are drawn with small pieces of SVG, and the
  counting is done by the database.
- Also added: scripts/seed_test_data.py, which fills a TEST database with fake
  responses (never run it on a live one).

## 1.11.0

- New: show a question only when an earlier answer matches. In a question's
  editor, "Show this question" > "Only if earlier answers match". Pick an
  earlier question, then "is", "is not", "is one of", "is at least" or "is at
  most", and the answer. Add several conditions and all of them must be true.
  Example: if the 0-10 score is at most 6, ask "What is the main reason for
  your score?". Questions with no rule behave exactly as before.
- A hidden question is not shown, not required and not sent. The server checks
  the rules again on every submission, so an answer posted to a hidden question
  is thrown away. A visitor's saved (reloaded) answers never bring back a hidden
  answer.
- Works in all three layouts: empty steps and screens are skipped, the "3 of 12"
  progress counts only questions that are showing, a grid row that is hidden
  disappears from the grid, and auto-advance goes to the next screen that shows.
- The editor warns when a rule points at a question that is below it, hidden or
  deleted (that question would stay hidden). The Questions list shows the same
  warning.
- New: link fields. Under Edit > Branding > Link fields, list names such as
  branch, source, staff. A link like /f/main/?branch=dubai&source=qr then saves
  those values with the response (plain text, 100 characters at most; any other
  name is ignored). They show in the Submissions list ("From link") and as extra
  columns in the CSV, for example "branch (link)".

## 1.10.0

- New: choose how each form is laid out, under Edit > Branding > Layout.
  "One page" is the old look and is what every existing form keeps. "Steps"
  shows groups of questions with Next and Back. "One question at a time" shows
  one question per screen. New forms start as one question at a time.
- Steps follow the question groups (categories), or wherever you press
  "Step break" next to a question in the Questions list.
- One at a time: a tap on a rating or single-choice answer moves on after a
  short pause. Enter goes next, number keys (and A, B, C for button answers)
  pick an answer, and the Up arrow goes back. A grid of questions is one
  screen and moves on once every row is answered.
- A progress bar with "3 of 12" (and Arabic wording) runs along the top and
  fills from the reading side.
- Optional welcome screen (title, text, Start button) and thank-you screen
  (title, text, optional button with a link), each in English and Arabic.
  The text boxes use the same editor as the description.
- Answers are kept when you go back, switch language or reload the page
  mid-form (they live only in that browser tab and are cleared once sent).
- The form is still sent once at the end, exactly as before.
- Fixed: the "0 / 1000" counter under a long-answer box could throw an error
  on forms with questions after it.

## 1.9.0

- New: rating and multiple-choice questions can be tapped instead of picked
  from a dropdown. Under Edit field, "Shown as" now offers: Stars, Faces (five
  drawn faces), Number buttons (1-5), a Number scale (0-10, 1-10 or 1-5, with
  "Not likely / Extremely likely" under a 0-10 scale), Choice buttons (for
  questions with up to 6 answers) and a Grid. A small preview shows what
  visitors will see.
- Grid: neighbouring questions with the same answers (for example the
  Excellent to Very poor ones) become one table. On a phone each question
  turns into its own card.
- The buttons work with the keyboard (arrow keys, Space or Enter), have
  spoken labels, flip correctly in Arabic, follow the form's font, text size,
  light or dark look and main colour, and show the usual red message when a
  required one is missed.
- Answers are saved exactly as before, so exports and old results are
  unchanged. Old forms keep their dropdowns and stars until you change them.
- New forms made from a template now use the 0-10 scale for the recommend
  question and Stars for the overall rating.

## 1.8.0

- New: every form has its own title and description, written separately in
  English and Arabic under Edit > Branding. The title is the big heading on the
  form and the browser tab (blank = the form's name). It no longer adds
  "- Feedback" to the tab.
- The description now has a small editor: bold, italic, underline, links and
  bulleted lists. Anything else is stripped out when you save, so it is safe.
  Old subtitles became descriptions automatically and look the same.
- New: pick a font (six that cover both English and Arabic), a text size
  (small, normal, large) and where the title sits (start or centered). The
  fonts are built in, nothing is loaded from the internet.
- Branding has a live preview with an EN / AR switch.
- Forms made from a template (and the first form on a fresh install) now get
  the template's title and description in both languages.
- Fix: on a phone, the Arabic form could be dragged sideways. It no longer can.

## 1.7.0

- The app is now neutral: all wording about clubs, fans, matches and stadiums
  is gone, in English and Arabic. The privacy text uses your organisation name,
  and the marketing tick-box now reads "Yes, keep me updated with news and offers."
- New: when you click "Add a form" you first pick a starting point: Blank,
  Customer satisfaction, Net Promoter Score, Event feedback, Product feedback,
  Employee feedback, Website feedback, or Venue / visit experience. Each comes
  with ready-made questions and a short description, in English and Arabic.
- A fresh install's first form is now called "Feedback" and uses the Customer
  satisfaction questions.
- Nothing changes for forms you already have: their names, questions and
  answers stay exactly as they were.

## 1.6.4

- Removed the fixed heading "Tell us what you think" / "شاركنا رأيك" and the
  fixed description under it from every form. The top of the form now shows
  only the form's own subtitle, if one is set under Edit → Branding, and
  nothing at all when it isn't.

## 1.6.3

- Fixed: running the install link a second time could stop with "Port 8800 is
  already in use" — the port in use was the app's own. The installer mistook a
  stray container started from the app's image for the app itself. It now
  looks only at the container Docker Compose manages.

## 1.6.2

- Removed the fixed "Fan Voice" / "صوت الجمهور" line under the name at the top
  of every form. The header now shows just your logo and organisation name.

## 1.6.1

- Fixed: the admin panel showed a blank page from 1.5.0 on. A line break inside
  one of the Clone messages stopped the panel's script from loading at all.

## 1.6.0

- **The admin panel is always on port 8800.** It's fixed in
  `docker-compose.yml` rather than read from `.env`, so no update, re-cloned
  folder or rebuilt `.env` can move it. Updating an older install moves the
  admin panel from its old port to 8800 once — point your tunnel or bookmark
  at 8800. If something else on the server already uses 8800, the installer
  stops and says so before changing anything. Forms keep their ports.
- **Light, dark or both, per form** (Edit → Branding → Light or dark). "Light
  only" or "Dark only" keeps the form that way on every device and hides the
  switch button; "Both" follows the visitor's device and lets them switch, as
  every form did until now — so existing forms look the same.

## 1.5.2

- **The admin panel's port no longer moves.** If `.env` went missing (a
  re-cloned folder, for instance) the installer rebuilt it from the example
  file, which put the admin panel back on 8080 even when the install was using
  another port. The installer now reads the port the install is actually
  published on, straight from the container, and writes it into `.env` so no
  later update can change it. A port you set yourself is never overwritten.
- The installer now prints the port map at the end — which host port reaches
  the admin panel, which reaches the first form, and the range for the rest.

## 1.5.1

- A **new form no longer asks for first name, last name and email** unless you
  tick that box — it starts with only the questions you choose. Forms that
  already exist are unchanged, and the consent tick is always there.
- When a form isn't collecting a name or email, the consent paragraph no
  longer says it is (English and Arabic).

## 1.5.0

- **One address for every form.** Each form is now also served through the
  admin panel's own address at `/f/<form-slug>/`, so a single tunnel or proxy
  hostname publishes all of them. A form keeps its own port as well — this is
  for when opening a port per form isn't practical, which is the usual reason
  a new form works on the server but not from outside.
- **Test port**, beside the Port box: says whether the port is free, whether
  it's the one auto-assign would pick, and whether Docker publishes it — before
  you save the form.
- **Clone** a form: same questions, languages and branding, on its own port.
  Submissions and alert rules are not copied.
- **My colors**, top right: each person can set their own admin-panel colors,
  saved to their account. Configuration → Appearance stays the colors everyone
  else starts from.
- **First name** and **Last name** are now questions in the catalog, in English
  and Arabic, under Personal details. If an admin had made their own
  (`firstname`, `surname`, …), the catalog entry is replaced by ours; their
  forms and everything already submitted are untouched.
- A question an admin named themselves for something we know about (`dob`,
  `mobile`, `mail`, …) is now grouped with the rest instead of sitting alone
  under Other.
- **A category is asked for on every question**, chosen from a list with room
  for your own — nothing fits, it goes to "Other".
- **The built-in First name / Last name / Email block can be turned off** per
  form (Manage → Built-in questions), so a form can ask only the questions you
  chose. On for every existing form, and the consent tick always stays.

## 1.4.0

- New question in the catalog: **Email address**, in English and Arabic. Every
  form already asks for an email at the top, so this one is for when you want
  to ask for a second address — it isn't added to a new form by default.
- Questions are now listed **Personal details first**, then About the visit,
  Experience ratings, Overall and Consent, in the catalog and in every
  category picker. Your own categories follow, and anything uncategorised is
  last.
- A form can have a **public address** (Forms → Manage): the hostname visitors
  really use, such as the Cloudflare Tunnel hostname pointed at that form. The
  View button and the link beside the form then open that address.
- Fixed: behind a tunnel or proxy, a form's link pointed at `0.0.0.0` — the
  address the app listens on, which a browser can't open. It now uses the
  hostname you reached the admin panel on, or the form's public address.
- Pick a port by hand and the panel now says when Docker isn't publishing it,
  so a form that starts but can't be reached is no longer a silent surprise.

## 1.3.0

- New question type: **Date**, with a proper calendar box on phones. Dates
  are checked on the way in, so "31 February" or a typed-in format can't be
  saved.
- New questions in the catalog: **Date of birth** and **Family name**. Email
  address and mobile number were already built in.
- The Arabic label for the built-in Last name is now الاسم الأخير, so it no
  longer reads the same as the new Family name (اسم العائلة).
- Questions now have a **category**, asked when you add one, and the catalog
  and the "add a question" list are grouped by it.
- You can **create a question while building a form** — it goes into the
  catalog and onto the form in one step.
- Questions added by a release appear in your catalog once, and never
  overwrite or duplicate a question you created yourself.
- An "Update available" marker now stays in the header until you update.

## 1.2.0

- Passwords: anyone can change their own from the top right, the owner
  account included; anyone who manages users can reset someone else's.
  Locked out? `reset_password.py` on the server.
- The owner account now appears in Administration → Users.
- Email: a "Send test email" button, and a "From name" so alerts arrive as
  your organisation's name instead of a bare address.
- Configuration → Backup: download one zip with the settings, the uploaded
  logos and a full copy of the database.
- Dashboard: submissions are drawn as a line graph.
- Installing and updating: the same link now updates an existing install
  without touching its data, finds the install from any folder, takes a
  safety backup first, and repairs permission problems by itself. Installs
  that use your own MariaDB (rather than the bundled one) now start
  correctly.
- Security: the settings file is now readable only by its owner, the admin
  login cookie is marked HTTPS-only behind a proxy, and uploaded logos are
  served locked down.

## 1.1.0

- Custom roles and users: build a role from a permission checklist and
  assign it to any number of people, per form.
- Alerts by email and Telegram: new submission, daily digest, database
  disconnected, form expiry.
- Forms can expire on a date, and disabled forms move to an Archive section
  with an export-first warning before permanent deletion.
- Dashboard: date filters, hourly view, and counts by form and language.
- Docker: one-line installer, and the admin panel asks for the database
  connection during first-run setup.

## 1.0.0

- First release: bilingual (English/Arabic) feedback forms, one admin panel,
  any number of forms each on its own port, per-form branding and questions,
  CSV export, honeypot and rate limiting.
