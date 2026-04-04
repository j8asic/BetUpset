import time
import requests

t0 = time.time()
r = requests.get("https://clob.polymarket.com/books?token_ids=0")
t1 = time.time()
print(f"Time: {t1-t0:.2f}s, status: {r.status_code}")
