import argparse
from app import get_db, execute
from werkzeug.security import generate_password_hash

p=argparse.ArgumentParser(description="Create or update a local GABAY administrator")
p.add_argument('--email', required=True)
p.add_argument('--password', required=True)
p.add_argument('--name', default='GABAY Administrator')
a=p.parse_args()
conn=get_db()
try:
    existing=execute(conn,'SELECT id FROM users WHERE lower(email)=?',(a.email.strip().lower(),)).fetchone()
    if existing:
        execute(conn,"UPDATE users SET full_name=?, password=?, role='admin' WHERE id=?",(a.name.strip(),generate_password_hash(a.password),existing['id']))
    else:
        execute(conn,"INSERT INTO users(full_name,email,password,role,profile_image) VALUES(?,?,?,?,NULL)",(a.name.strip(),a.email.strip().lower(),generate_password_hash(a.password),'admin'))
    conn.commit()
    print(f'Administrator ready: {a.email.strip().lower()}')
finally:
    conn.close()
