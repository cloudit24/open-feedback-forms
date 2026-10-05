# Changelog

What changed in each release, in plain English. The admin panel reads this
file from GitHub to show what a new version fixes.

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
