# QueueLess – Smart Virtual Queue Management

Book bank tokens online, track your live queue position, and let admins manage banks, branches, services, reports and notifications.

## Run
```
cd backend
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000 . The database (`backend/database/database.db`) is created and seeded automatically.

| Role  | Default login      |
|-------|--------------------|
| Admin | `admin` / `admin123` |
| User  | `user` / `user123`   |

New users/admins can sign up at `/register`. Admin sign-up needs the secret key (default `QUEUELESS-ADMIN-2026`; change it with the `ADMIN_REGISTER_KEY` environment variable).

## Features
- **User:** register, book token (bank → branch → service, date/time), live queue position + estimated wait, cancel/remove tokens, notification inbox.
- **Admin:** reports with filters, serve/cancel tokens, CSV export & print, analytics, send notifications, manage banks / branches / services / admins, max queue size.

Stack: Flask + SQLite (indexed, WAL mode), plain HTML/CSS.  `python init_db.py --reset` wipes and re-creates the database.
