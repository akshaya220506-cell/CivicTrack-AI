# CivicTrack AI on Render (demo mode)

The checked-in `render.yaml` describes a free Render web service. It starts the
Flask app with Gunicorn and enables `DEMO_MODE=true`. Demo mode chooses a
separate database and uploads directory under the operating system's temporary
directory. It does not load this project's `.env` or open its local database,
uploads, or backups.

## Before deploying

1. Keep `.env`, SQLite files, uploaded photos, and backups out of Git. The
   project's `.gitignore` already excludes these local files.
2. Generate an admin password hash locally without putting the password in a
   command history:

   ```powershell
   python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass('Admin password: ')))"
   ```

3. When creating the Render Blueprint, enter a private `ADMIN_USERNAME`, the
   generated `ADMIN_PASSWORD_HASH`, and optionally a Gemini API key when Render
   asks for the `sync: false` variables. Render generates `SECRET_KEY` for the
   service. Never put those secret values in `render.yaml` or Git.
4. Choose the Free plan. The service uses `DEMO_MODE=true` regardless of your
   local `.env` setting.

## Demo data and storage limits

The dashboard includes three clearly labeled fictional complaints. They have
no citizen names, phone numbers, exact addresses, or photos. Their IDs can be
entered on the public tracking page.

Render Free web services have an ephemeral filesystem. Local SQLite changes
and uploaded photos can be lost when a service spins down, restarts, or
redeploys. Demo samples are re-created at startup, but citizen reports and
uploaded photos are not durable on this configuration. Use a persistent
database and object storage before using the site for real public complaints.

## Local development

Leave `DEMO_MODE` unset or set it to `false` in the shell. The existing local
`python app.py` workflow continues to use `civictrack.db`, `uploads/`, and the
local `.env` as before. Do not set `DEMO_MODE=true` in the local `.env` if you
expect local app runs to load that file; demo mode intentionally skips it.
