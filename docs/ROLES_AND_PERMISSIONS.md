# A flexible roles/permissions pattern

A generic way to let an application owner hand out restricted accounts —
"this person can only see and manage *their* thing" — without hardcoding a
fixed list of role names (Admin/Editor/Viewer) that inevitably stops fitting
once someone needs something in between. This document describes the
pattern itself, not any one app's table names or permission keys — see
that app's own code for its concrete list (in this project:
`server.py`'s `GLOBAL_PERMISSIONS`/`FORM_PERMISSIONS` constants).

## The core idea: a checklist, not an enum

Instead of a fixed set of roles, define a fixed set of **permissions** —
short, specific, named for an action ("export submissions", not "editor").
A **role** is just a name plus whichever permissions are checked. Anyone
can create a new role by ticking boxes; nobody has to touch code to add
"Read-only" or "Department Head" or any other role a real organization
turns out to need. This is the entire trick — everything else below is
detail that falls out of it.

## Two kinds of permission

Most apps that need this have some notion of a "resource" a permission
might apply to broadly or narrowly — a form, a project, a document, a
customer account. Split permissions into two buckets:

- **Global permissions** — meaningful without reference to any specific
  resource: managing users themselves, application-wide settings,
  infrastructure credentials. A role either has one of these or it doesn't.
- **Resource-scoped permissions** — meaningful only *for* a particular
  resource: viewing X's data, editing X's settings. A role has the
  permission in the abstract ("can edit"), and separately, each **user**
  with that role is assigned *which* resources it applies to.

A role also carries one boolean that short-circuits the scoping question
entirely: **applies-to-all**. A role with it on ignores per-user resource
assignment for all its scoped permissions — useful for a small "trusted
staff" role that should just see everything without an admin manually
attaching every resource to every such user. A role with it off requires
each user to be individually assigned the specific resources they can
touch.

```
Role   = { name, permissions: [...keys], applies_to_all: bool }
User   = { username, credentials, role_id }
        + (only meaningful when role.applies_to_all is false)
          assigned_resource_ids: [...]
```

## Checking a permission at request time

Resolve the user's role and resource assignments **once, at login**, into
a plain in-memory structure attached to their session — not a database
query per permission check. A session becomes:

```
{ permissions: set(...), resource_ids: set(...) | "all" }
```

Then every check is pure and cheap:

```
def has_permission(session, key, resource_id=None):
    if key not in session.permissions:
        return False
    if resource_id is not None and session.resource_ids != "all" \
            and resource_id not in session.resource_ids:
        return False
    return True
```

A bootstrap/superuser account (see below) should short-circuit this to
always return true, rather than being modeled as "a role with every
permission" — it exists for a different reason than the rest of the
system and shouldn't have to stay in sync with the permission list as it
grows.

## The bootstrap account is not a role

Systems like this usually have a chicken-and-egg problem: something has to
be able to create the *first* user, configure the database this whole
permission system might itself live in, and recover access if every
custom role gets misconfigured into uselessness. Keep exactly one
account — created during initial setup, stored wherever the app's most
foundational config already lives (a local config file, an environment
variable, whatever is reachable before the rest of the system is even
up) — that always has full access and is never subject to the permission
checklist at all. Don't try to express "full access" as a role with every
box checked; a new permission added six months from now should be
automatically covered by the bootstrap account without anyone remembering
to update a "SuperAdmin" role's checklist to match.

## Enforce twice: hide, and also block

The UI should hide any control the current user's permissions don't cover
— nobody should see a button that will just 403. But **that is a UX
courtesy, never the actual security boundary.** Every server-side
handler for a permission-gated action must perform its own check,
independent of whatever the client claims or sends. A user who crafts a
request by hand (or a bug in the UI's hiding logic) must hit the same
wall a real attacker would. If a check is missing on the server, the
feature is not actually gated, no matter how thoroughly the UI hides it.

## When a "view all" listing meets scoped access

A common trap: an endpoint that lists/searches across every resource
(e.g. "all submissions") needs a different answer depending on caller.
Don't just gate the endpoint on/off — when the caller's role scopes them
to specific resources, **narrow the underlying query** to that set rather
than filtering results after the fact (that's both a performance problem
at scale and an easy place to introduce a leak if the filter is ever
forgotten on one code path). Concretely: if a request already names one
resource, check that specific one; if it asks for "everything," rewrite
the query as "everything the caller is allowed to see," never as
"everything, full stop."

## What doesn't belong in the permission checklist

Business rules that happen to correlate with a role (e.g. "only a
department head can trigger X") are not automatically permissions in this
system — resist the urge to add a new permission key for every rule.
Reserve the checklist for genuine access-control decisions: can this
person see this, can they change that. If a rule is really about
workflow (approval chains, sign-off order) rather than visibility or
mutation rights, model it separately; folding it into the permission
checklist makes the checklist long and the meaning of any one box fuzzy.
