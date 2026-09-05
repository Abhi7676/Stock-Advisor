import sqlite3, os
p='stock_market.sqlite'
if os.path.exists(p):
    conn=sqlite3.connect(p)
    cur=conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        print('Tables in',p)
        for r in cur.fetchall():
            print('-',r[0])
    except Exception as e:
        print('sqlite error',e)
    conn.close()
else:
    print(p,'not found')

p2='stock_market.duckdb'
print('\nDuckDB file exists?' , os.path.exists(p2))
