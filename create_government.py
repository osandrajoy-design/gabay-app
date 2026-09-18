import argparse
from app import get_db, execute
from werkzeug.security import generate_password_hash

p=argparse.ArgumentParser(description="Create or update a local GABAY government personnel account")
p.add_argument('--email', required=True)
p.add_argument('--password', required=True)
p.add_argument('--name', default='Government Personnel')
p.add_argument('--role', default='government', choices=['government','gov_employee','government_employee'])
a=p.parse_args()
conn=get_db()
try:
    existing=execute(conn,'SELECT id FROM users WHERE lower(email)=?',(a.email.strip().lower(),)).fetchone()
    if existing:
        execute(conn,"UPDATE users SET full_name=?, password=?, role=? WHERE id=?",(a.name.strip(),generate_password_hash(a.password),a.role,existing['id']))
    else:
        execute(conn,"INSERT INTO users(full_name,email,password,role,profile_image) VALUES(?,?,?,?,NULL)",(a.name.strip(),a.email.strip().lower(),generate_password_hash(a.password),a.role))
    conn.commit()
    print(f'Government account ready: {a.email.strip().lower()} ({a.role})')
finally:
    conn.close()
