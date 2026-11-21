import hashlib
import hmac
import json
import os
from typing import Optional
from urllib.parse import parse_qsl

import firebase_admin
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, firestore
from pydantic import BaseModel

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
FIREBASE_CRED_PATH = os.environ.get("FIREBASE_CRED_PATH", "serviceAccountKey.json")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

if not firebase_admin._apps:
    if FIREBASE_CREDENTIALS_JSON:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
    else:
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()
app = FastAPI(title="ZooMarket API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=403, detail="Invalid initData signature")

    user_raw = parsed.get("user")
    return json.loads(user_raw) if user_raw else {}


class AdCreate(BaseModel):
    kind: str
    title: str
    category: str
    price: str = ""
    currency: str = "руб."
    city: str = ""
    description: str = ""
    phone: str = ""
    contact: str = "Telegram"


@app.get("/")
def root():
    return {"status": "ok", "service": "ZooMarket API"}


@app.get("/api/ads")
def get_ads(
    category: Optional[str] = None,
    q: Optional[str] = Query(default=None),
):
    query = db.collection("ads").where("status", "==", "approved")
    docs = [doc.to_dict() | {"id": doc.id} for doc in query.stream()]

    if category and category != "Все":
        docs = [x for x in docs if x.get("category") == category]

    if q:
        q_low = q.lower().strip()
        docs = [
            x
            for x in docs
            if q_low
            in f"{x.get('title', '')} {x.get('description', '')} {x.get('city', '')}".lower()
        ]

    return {"items": docs}


@app.post("/api/ads")
def create_ad(ad: AdCreate, x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    title = ad.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    doc_data = {
        "kind": ad.kind,
        "title": title,
        "category": ad.category,
        "price": f"{ad.price.strip()} {ad.currency}".strip() if ad.price.strip() else ad.currency,
        "city": ad.city.strip(),
        "description": ad.description.strip(),
        "phone": ad.phone.strip(),
        "contact": ad.contact,
        "status": "pending",
        "author_id": tg_user.get("id"),
        "author_name": " ".join(
            part for part in [tg_user.get("first_name"), tg_user.get("last_name")] if part
        ),
    }

    _, ref = db.collection("ads").add(doc_data)
    return {"id": ref.id, "status": "pending"}


@app.get("/api/my-ads")
def get_my_ads(x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    author_id = tg_user.get("id")
    docs = db.collection("ads").where("author_id", "==", author_id).stream()
    return {"items": [doc.to_dict() | {"id": doc.id} for doc in docs]}