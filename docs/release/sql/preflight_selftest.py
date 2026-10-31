"""Disposable-DB self-test of the AST-005 preflight (original vs revision 2). Synthetic data only.

Usage (local disposable PostgreSQL only; refuses DB names not starting with rs_so):
    createdb rs_so_preflight
    PGPASSWORD=... python3 docs/release/sql/preflight_selftest.py <original.sql> docs/release/sql/prod_email_preflight.sql
See docs/release/PREFLIGHT_REVIEW.md section 3 for the expected output.
"""
import json, os, subprocess, sys
DB = "rs_so_preflight"
assert DB.startswith("rs_so")
URL = f"postgresql://rideshare_ci@localhost:5432/{DB}"
SEED = [
 (1,"alice@example.test"),(2,"ALICE@example.test"),                 # case twin (blocks)
 (3," bob@example.test"),(4,"bob@example.test"),                    # ASCII-space padded
 (5,"carol@example.test\u00a0"),(6,"Carol@example.test"),          # NBSP padded + case
 (7,"\u2003dave@example.test"),(8,"dave@example.test"),             # EM SPACE padded
 (9,"\u0130sa@example.test"),(10,"isa@example.test"),               # dotted capital I
 (11,"\u1e9etrasse@example.test"),(12,"\u00dftrasse@example.test"), # capital sharp s
 (13,"eve@example.test\t"),(14,"eve@example.test"),                 # tab padded
 (15,"\u200bfrank@example.test"),(16,"frank@example.test"),         # ZWSP (not whitespace in Python)
 (17,"grace@example.test\u3000"),(18,"GRACE@EXAMPLE.TEST"),        # ideographic space
 (19,"heidi@example.test"),                                          # clean
]
def psql(sql=None, f=None, check=True):
    cmd=["psql",URL,"-X","-qAt","-v","ON_ERROR_STOP=1"]+(["-f",f] if f else ["-c",sql])
    r=subprocess.run(cmd,capture_output=True,text=True)
    if check and r.returncode: raise SystemExit(r.stderr)
    return r
psql("DROP TABLE IF EXISTS users; CREATE TABLE users (id int PRIMARY KEY, email varchar NOT NULL)")
for i,e in SEED:
    psql("INSERT INTO users VALUES (%d, %s)" % (i, "'" + e.replace("'","''") + "'"))
res={}
for name,f in (("original",sys.argv[1]),("rev2",sys.argv[2])):
    r=psql(f=f)
    out=r.stdout.strip().splitlines()
    res[name]=out
    print(f"--- {name} (exit {r.returncode})"); print("\n".join(out))
    assert "example.test" not in r.stdout.lower(), "PII leaked"
# Ground truth for section 3: does uq_users_email_lower build? (it must not while twins exist)
r=psql("BEGIN; CREATE UNIQUE INDEX uq_test ON users (lower(email)); ROLLBACK;", check=False)
print("uq index builds with twins present:", r.returncode==0)
# Ground truth for section 4: Python str.strip() (the API's trim) then PostgreSQL lower().
groups={}
for i,e in SEED:
    k=psql("SELECT lower(%s)" % ("'"+e.strip().replace("'","''")+"'")).stdout.strip()
    groups.setdefault(k,[]).append(i)
truth=sorted(v for v in groups.values() if len(v)>1)
print("python-parity trim+lower groups:", truth)
# Ground truth for section 3: PG lower() without trim
groups={}
for i,e in SEED:
    groups.setdefault(psql("SELECT lower(%s)" % ("'"+e.replace("'","''")+"'")).stdout.rstrip("\n"),[]).append(i)
print("lower() groups:", sorted(v for v in groups.values() if len(v)>1))
print("python padded ids:", [i for i,e in SEED if e!=e.strip()])
# read-only proof: an INSERT inside the rev2 transaction must fail
r=psql("BEGIN TRANSACTION READ ONLY; INSERT INTO users VALUES (99,'x@example.test'); ROLLBACK;", check=False)
print("write inside READ ONLY txn rejected:", r.returncode!=0 and "read-only" in r.stderr)
print("row count unchanged:", psql("SELECT count(*) FROM users").stdout.strip()==str(len(SEED)))

# Agreement after resolving the blocking twins: section 3 must read 0 and the index must build.
psql("DELETE FROM users WHERE id IN (2, 10, 12)")
r=psql(f=sys.argv[2]).stdout.strip().splitlines()
sec3=[l for l in r if l.startswith("0|[]")]
b=psql("BEGIN; CREATE UNIQUE INDEX uq_test ON users (lower(email)); ROLLBACK;", check=False)
print("after resolving twins: rev2 section 3 reports 0 groups:", bool(sec3), "| uq index builds:", b.returncode==0)
