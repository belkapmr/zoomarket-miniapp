import hashlib
import hmac
import json
import os
from typing import List, Optional
from urllib.parse import parse_qsl

import firebase_admin
from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, firestore
from pydantic import BaseModel

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9022361491:AAEz6y0zvihhwkcZQOhm-1UXS_Q91Hmufzc")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
FIREBASE_CRED_PATH = os.environ.get(
    "FIREBASE_CRED_PATH", "zoomarket-121d2-firebase-adminsdk-fbsvc-ac9a0145b5.json"
)

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
    photos: List[str] = []


class AdUpdate(BaseModel):
    kind: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    city: Optional[str] = None
    description: Optional[str] = None
    phone: Optional[str] = None
    contact: Optional[str] = None
    photos: Optional[List[str]] = None


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
        "photos": (ad.photos or [])[:3],
        "status": "approved",
        "author_id": tg_user.get("id"),
        "author_name": " ".join(
            part for part in [tg_user.get("first_name"), tg_user.get("last_name")] if part
        ),
    }

    _, ref = db.collection("ads").add(doc_data)
    return {"id": ref.id, "status": "approved", "item": doc_data | {"id": ref.id}}


@app.patch("/api/ads/{ad_id}")
def update_ad(
    ad_id: str,
    payload: AdUpdate = Body(...),
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    user_id = tg_user.get("id")

    doc_ref = db.collection("ads").document(ad_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Ad not found")

    data = snap.to_dict() or {}
    if str(data.get("author_id", "")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden: not the author")

    updates = {}
    if payload.kind is not None:
        updates["kind"] = payload.kind
    if payload.title is not None:
        clean_title = payload.title.strip()
        if not clean_title:
            raise HTTPException(status_code=400, detail="Title is required")
        updates["title"] = clean_title
    if payload.category is not None:
        updates["category"] = payload.category
    if payload.city is not None:
        updates["city"] = payload.city.strip()
    if payload.description is not None:
        updates["description"] = payload.description.strip()
    if payload.phone is not None:
        updates["phone"] = payload.phone.strip()
    if payload.contact is not None:
        updates["contact"] = payload.contact
    if payload.photos is not None:
        updates["photos"] = payload.photos[:3]
    if payload.price is not None:
        p = payload.price.strip()
        curr = (payload.currency or data.get("currency") or "").strip()
        updates["price"] = f"{p} {curr}".strip() if p and curr and curr not in p else (p or curr)

    if updates:
        doc_ref.update(updates)

    updated = doc_ref.get().to_dict() or {}
    return {"id": ad_id, "item": updated | {"id": ad_id}}


@app.delete("/api/ads/{ad_id}")
def delete_ad(ad_id: str, x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    user_id = tg_user.get("id")

    doc_ref = db.collection("ads").document(ad_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Ad not found")

    data = snap.to_dict() or {}
    if str(data.get("author_id", "")) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden: not the author")

    doc_ref.delete()
    return {"status": "deleted", "id": ad_id}


@app.get("/api/my-ads")
def get_my_ads(x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    author_id = tg_user.get("id")
    docs = db.collection("ads").where("author_id", "==", author_id).stream()
    return {"items": [doc.to_dict() | {"id": doc.id} for doc in docs]}
