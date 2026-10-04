"""Optional helper. The app creates/migrates its own database on start-up.

python init_db.py            -> create missing tables, keep all data
python init_db.py --reset    -> DELETE everything and start fresh (default admin/user are re-created)
"""
import sys
from app import init_schema

init_schema(reset="--reset" in sys.argv)
print("Database ready.  Default logins -> admin / admin123   |   user / user123")
