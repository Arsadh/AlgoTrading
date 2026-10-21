"""Start the dashboard: python run.py  ->  http://127.0.0.1:3001 (override with PORT in .env)

Binds to localhost only. Do not expose this to the internet without adding
authentication: it can place real orders.
"""
import os

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv()
    port = int(os.getenv("PORT", "3001"))
    uvicorn.run("app.api:app", host="127.0.0.1", port=port, reload=False)
