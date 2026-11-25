import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

import firebase_admin
from fastapi import Body, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, firestore
from pydantic import BaseModel, Field

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9022361491:AAEz6y0zvihhwkcZQOhm-1UXS_Q91Hmufzc")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
FIREBASE_CRED_PATH = os.environ.get(
    "FIREBASE_CRED_PATH", "zoomarket-121d2-firebase-adminsdk-fbsvc-ac9a0145b5.json"
)
ALLOW_UNVERIFIED_DEV = os.environ.get("ALLOW_UNVERIFIED_DEV", "").strip().lower() in ("1", "true", "yes")

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")

if not firebase_admin._apps:
    if FIREBASE_CREDENTIALS_JSON:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
    elif os.path.exists(FIREBASE_CRED_PATH):
        cred = credentials.Certificate(FIREBASE_CRED_PATH)
    else:
        cred = credentials.ApplicationDefault()
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


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def parse_iso_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def verify_telegram_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data:
        if ALLOW_UNVERIFIED_DEV:
            return {"id": "dev_user", "first_name": "Dev", "username": "dev_user"}
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


def optional_tg_user(x_telegram_init_data: str = "") -> Optional[dict]:
    if not x_telegram_init_data:
        return None
    try:
        return verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    except HTTPException:
        return None


def send_telegram_message(chat_id: str, text: str, reply_markup: Optional[dict] = None) -> bool:
    clean_chat = str(chat_id or "").strip()
    if not clean_chat or not BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": clean_chat,
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except Exception:
        return False


def resolve_telegram_chat_id(
    user_id: Optional[str] = None,
    username: Optional[str] = None,
) -> Optional[str]:
    clean_id = str(user_id or "").strip()
    if clean_id.isdigit():
        return clean_id

    clean_user = str(username or "").lstrip("@").strip().lower()
    if not clean_user:
        return None

    try:
        pref_snap = db.collection("notification_preferences").document(f"user_{clean_user}").get()
        if pref_snap.exists:
            pdata = pref_snap.to_dict() or {}
            uid = str(pdata.get("user_id") or "").strip()
            if uid.isdigit():
                return uid
    except Exception:
        pass

    try:
        for col in ("ads", "shop_orders", "grooming_bookings", "vet_appointments"):
            for doc in db.collection(col).limit(200).stream():
                d = doc.to_dict() or {}
                for id_key, u_key in (
                    ("owner_id", "owner_username"),
                    ("seller_id", "seller_username"),
                    ("author_id", "author_username"),
                    ("groomer_id", "groomer_username"),
                    ("vet_id", "vet_username"),
                    ("buyer_id", "buyer_username"),
                    ("client_id", "client_username"),
                ):
                    u_val = str(d.get(u_key) or "").lstrip("@").strip().lower()
                    i_val = str(d.get(id_key) or "").strip()
                    if u_val == clean_user and i_val.isdigit():
                        return i_val
    except Exception:
        pass

    return None


def parse_price_number(price_val: Any) -> float:
    if isinstance(price_val, (int, float)):
        return float(price_val) if float(price_val) >= 0 else 0.0
    s = str(price_val or "").strip().lower()
    if not s or "бесплат" in s or "даром" in s:
        return 0.0
    match = re.search(r"\d+(?:[.,]\d+)?", s.replace(" ", ""))
    if not match:
        return 0.0
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return 0.0


def format_price_string(price_val: Optional[str], currency_val: Optional[str]) -> str:
    p = (price_val or "").strip()
    c = (currency_val or "").strip()
    if c.lower() == "бесплатно" and not p:
        return "Бесплатно"
    if p and c and c.lower() not in p.lower():
        return f"{p} {c}".strip()
    return p or c


def is_ad_owner(data: Dict[str, Any], user_id: Any, username: str = "") -> bool:
    uid_str = str(user_id or "").strip()
    uname_low = str(username or "").lstrip("@").strip().lower()
    if uid_str:
        for k in ("author_id", "owner_id", "seller_id"):
            if str(data.get(k) or "").strip() == uid_str:
                return True
    if uname_low:
        for k in ("author_username", "owner_username", "seller_username"):
            if str(data.get(k) or "").lstrip("@").strip().lower() == uname_low:
                return True
    return False


# --- Pydantic Models ---

class AdCreate(BaseModel):
    kind: str
    title: str
    category: str
    price: str = ""
    currency: str = "руб."
    stock: Optional[int] = 1
    city: str = ""
    description: str = ""
    phone: str = ""
    contact: str = "Telegram"
    photos: List[str] = Field(default_factory=list)
    is_shop: bool = False
    shop_id: Optional[str] = None
    shop_name: Optional[str] = None
    is_grooming: bool = False
    grooming_dates: List[str] = Field(default_factory=list)
    grooming_times: List[str] = Field(default_factory=list)
    grooming_schedule_by_date: Dict[str, List[str]] = Field(default_factory=dict)
    is_vet: bool = False
    vet_dates: List[str] = Field(default_factory=list)
    vet_times: List[str] = Field(default_factory=list)
    vet_schedule_by_date: Dict[str, List[str]] = Field(default_factory=dict)
    owner_id: Optional[str] = None
    owner_username: Optional[str] = None
    seller_id: Optional[str] = None
    seller_name: Optional[str] = None
    seller_username: Optional[str] = None


class AdUpdate(BaseModel):
    kind: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    price: Optional[str] = None
    currency: Optional[str] = None
    stock: Optional[int] = None
    city: Optional[str] = None
    description: Optional[str] = None
    phone: Optional[str] = None
    contact: Optional[str] = None
    photos: Optional[List[str]] = None
    is_hidden: Optional[bool] = None
    status: Optional[str] = None
    is_shop: Optional[bool] = None
    is_grooming: Optional[bool] = None
    grooming_dates: Optional[List[str]] = None
    grooming_times: Optional[List[str]] = None
    grooming_schedule_by_date: Optional[Dict[str, List[str]]] = None
    is_vet: Optional[bool] = None
    vet_dates: Optional[List[str]] = None
    vet_times: Optional[List[str]] = None
    vet_schedule_by_date: Optional[Dict[str, List[str]]] = None


class BoostRequest(BaseModel):
    hours: int = 24
    boosted_at: Optional[str] = None
    boosted_until: Optional[str] = None


class ReviewCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    text: str
    author_name: Optional[str] = None


class OrderItemModel(BaseModel):
    ad_id: Optional[str] = None
    shop_id: Optional[str] = None
    shop_name: Optional[str] = None
    owner_id: Optional[str] = None
    owner_username: Optional[str] = None
    title: str
    price: Optional[str] = "0"
    unit_price: Optional[float] = 0.0
    category: Optional[str] = ""
    max_stock: Optional[int] = None
    qty: int = Field(ge=1)


class ShopOrderCreate(BaseModel):
    shop_id: Optional[str] = None
    shop_name: Optional[str] = "Магазин"
    owner_id: Optional[str] = None
    owner_username: Optional[str] = None
    buyer_id: Optional[str] = None
    buyer_username: Optional[str] = None
    buyer_name: str
    buyer_phone: str
    delivery_type: str = "Самовывоз"
    delivery_address: str = ""
    comment: str = ""
    items: List[OrderItemModel]


class GroomingBookingCreate(BaseModel):
    ad_id: Optional[str] = None
    groomer_id: Optional[str] = None
    groomer_name: Optional[str] = "Мастер"
    groomer_phone: Optional[str] = ""
    groomer_username: Optional[str] = None
    owner_id: Optional[str] = None
    owner_username: Optional[str] = None
    service_title: Optional[str] = "Груминг"
    date: str
    time: str
    pet_breed: Optional[str] = ""
    duration: Optional[str] = "90 мин"
    pet_and_tasks: str
    client_id: Optional[str] = None
    client_username: Optional[str] = None
    client_name: str
    client_phone: str


class GroomingCancelBody(BaseModel):
    cancelled_by: Optional[str] = None


class VetAppointmentCreate(BaseModel):
    ad_id: Optional[str] = None
    vet_id: Optional[str] = None
    vet_name: Optional[str] = "Ветеринар"
    vet_phone: Optional[str] = ""
    vet_username: Optional[str] = None
    owner_id: Optional[str] = None
    owner_username: Optional[str] = None
    service_title: Optional[str] = "Приём ветеринара"
    date: str
    time: str
    pet_species: str
    pet_weight: Optional[str] = ""
    problem: str
    client_id: Optional[str] = None
    client_username: Optional[str] = None
    client_name: str
    client_phone: str


class StatusUpdateBody(BaseModel):
    status: str


class NotificationPreferencesBody(BaseModel):
    user_id: Optional[str] = None
    username: Optional[str] = None
    preferences: Dict[str, bool] = Field(default_factory=dict)


class NotificationDispatchBody(BaseModel):
    event: str
    target_user_id: Optional[str] = None
    target_username: Optional[str] = None
    actor_id: Optional[str] = None
    actor_username: Optional[str] = None
    actor_name: Optional[str] = None
    preferences: Dict[str, bool] = Field(default_factory=dict)
    data: Dict[str, Any] = Field(default_factory=dict)


# --- Routes ---

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
    docs = [x for x in docs if not x.get("is_hidden")]

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

    tg_uid = str(tg_user.get("id") or "")
    tg_uname = str(tg_user.get("username") or "").lstrip("@")
    full_name = " ".join(
        part for part in [tg_user.get("first_name"), tg_user.get("last_name")] if part
    ).strip()

    stock_val = max(0, int(ad.stock)) if ad.stock is not None else 1
    owner_id = (ad.owner_id or ad.seller_id or tg_uid).strip() or tg_uid
    owner_username = (ad.owner_username or ad.seller_username or tg_uname).lstrip("@").strip()
    seller_name = (ad.seller_name or ad.shop_name or full_name or "Мастер / Продавец").strip()

    doc_data: Dict[str, Any] = {
        "kind": ad.kind,
        "title": title,
        "category": ad.category,
        "price": format_price_string(ad.price, ad.currency),
        "currency": ad.currency,
        "stock": stock_val,
        "city": ad.city.strip(),
        "description": ad.description.strip(),
        "phone": ad.phone.strip(),
        "contact": ad.contact,
        "photos": (ad.photos or [])[:3],
        "is_shop": bool(ad.is_shop),
        "shop_id": (ad.shop_id or (tg_uid if ad.is_shop else "")),
        "shop_name": (ad.shop_name or seller_name) if ad.is_shop else "",
        "is_grooming": bool(ad.is_grooming),
        "grooming_dates": ad.grooming_dates or [],
        "grooming_times": ad.grooming_times or [],
        "grooming_schedule_by_date": ad.grooming_schedule_by_date or {},
        "is_vet": bool(ad.is_vet),
        "vet_dates": ad.vet_dates or [],
        "vet_times": ad.vet_times or [],
        "vet_schedule_by_date": ad.vet_schedule_by_date or {},
        "owner_id": owner_id,
        "owner_username": owner_username,
        "seller_id": (ad.seller_id or tg_uid),
        "seller_name": seller_name,
        "seller_username": owner_username,
        "author_id": tg_user.get("id"),
        "author_name": full_name,
        "author_username": tg_uname,
        "is_hidden": False,
        "status": "approved",
        "created_at": iso_now(),
    }

    _, ref = db.collection("ads").add(doc_data)
    created = doc_data | {"id": ref.id}
    return {"id": ref.id, "status": "approved", "item": created}


@app.patch("/api/ads/{ad_id}")
def update_ad(
    ad_id: str,
    payload: AdUpdate = Body(...),
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    user_id = tg_user.get("id")
    uname = tg_user.get("username") or ""

    doc_ref = db.collection("ads").document(ad_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Ad not found")

    data = snap.to_dict() or {}
    if not is_ad_owner(data, user_id, uname):
        raise HTTPException(status_code=403, detail="Forbidden: not the author")

    updates: Dict[str, Any] = {}
    if payload.kind is not None:
        updates["kind"] = payload.kind
    if payload.title is not None:
        clean_title = payload.title.strip()
        if not clean_title:
            raise HTTPException(status_code=400, detail="Title is required")
        updates["title"] = clean_title
    if payload.category is not None:
        updates["category"] = payload.category
    if payload.currency is not None:
        updates["currency"] = payload.currency
    if payload.stock is not None:
        updates["stock"] = max(0, int(payload.stock))
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
    if payload.is_hidden is not None:
        updates["is_hidden"] = bool(payload.is_hidden)
        if payload.status is None:
            updates["status"] = "hidden" if payload.is_hidden else "approved"
    if payload.status is not None:
        updates["status"] = payload.status
        if payload.is_hidden is None:
            updates["is_hidden"] = payload.status == "hidden"
    if payload.is_shop is not None:
        updates["is_shop"] = bool(payload.is_shop)
    if payload.is_grooming is not None:
        updates["is_grooming"] = bool(payload.is_grooming)
    if payload.grooming_dates is not None:
        updates["grooming_dates"] = payload.grooming_dates
    if payload.grooming_times is not None:
        updates["grooming_times"] = payload.grooming_times
    if payload.grooming_schedule_by_date is not None:
        updates["grooming_schedule_by_date"] = payload.grooming_schedule_by_date
    if payload.is_vet is not None:
        updates["is_vet"] = bool(payload.is_vet)
    if payload.vet_dates is not None:
        updates["vet_dates"] = payload.vet_dates
    if payload.vet_times is not None:
        updates["vet_times"] = payload.vet_times
    if payload.vet_schedule_by_date is not None:
        updates["vet_schedule_by_date"] = payload.vet_schedule_by_date
    if payload.price is not None:
        p = payload.price.strip()
        curr = (payload.currency or data.get("currency") or "").strip()
        updates["price"] = format_price_string(p, curr)

    if updates:
        updates["updated_at"] = iso_now()
        doc_ref.update(updates)

    updated = doc_ref.get().to_dict() or {}
    return {"id": ad_id, "item": updated | {"id": ad_id}}


@app.post("/api/ads/{ad_id}/boost")
def boost_ad(
    ad_id: str,
    payload: BoostRequest = Body(default=BoostRequest()),
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    user_id = tg_user.get("id")
    uname = tg_user.get("username") or ""

    doc_ref = db.collection("ads").document(ad_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Ad not found")

    data = snap.to_dict() or {}
    if not is_ad_owner(data, user_id, uname):
        raise HTTPException(status_code=403, detail="Forbidden: not the author")

    current_dt = now_utc()
    last_boost_dt = parse_iso_dt(data.get("boosted_at"))
    if last_boost_dt and (current_dt - last_boost_dt) < timedelta(hours=24):
        remaining = timedelta(hours=24) - (current_dt - last_boost_dt)
        mins = max(1, int(remaining.total_seconds() // 60))
        raise HTTPException(
            status_code=429,
            detail=f"Бесплатное поднятие доступно 1 раз в 24 часа (осталось ~{mins} мин.)",
        )

    hours = max(1, min(168, int(payload.hours or 24)))
    boosted_at_dt = current_dt
    boosted_until_dt = current_dt + timedelta(hours=hours)

    updates = {
        "boosted_at": boosted_at_dt.isoformat(),
        "boosted_until": boosted_until_dt.isoformat(),
    }
    doc_ref.update(updates)
    updated = doc_ref.get().to_dict() or {}
    return {
        "id": ad_id,
        "boosted_at": updates["boosted_at"],
        "boosted_until": updates["boosted_until"],
        "item": updated | {"id": ad_id},
    }


@app.delete("/api/ads/{ad_id}")
def delete_ad(ad_id: str, x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    user_id = tg_user.get("id")
    uname = tg_user.get("username") or ""

    doc_ref = db.collection("ads").document(ad_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Ad not found")

    data = snap.to_dict() or {}
    if not is_ad_owner(data, user_id, uname):
        raise HTTPException(status_code=403, detail="Forbidden: not the author")

    doc_ref.delete()
    return {"status": "deleted", "id": ad_id}


@app.get("/api/my-ads")
def get_my_ads(x_telegram_init_data: str = Header(default="")):
    tg_user = verify_telegram_init_data(x_telegram_init_data, BOT_TOKEN)
    author_id = tg_user.get("id")
    docs = db.collection("ads").where("author_id", "==", author_id).stream()
    return {"items": [doc.to_dict() | {"id": doc.id} for doc in docs]}


# --- Sellers & Reviews ---

@app.get("/api/sellers/{seller_id}")
def get_seller_profile(seller_id: str):
    clean_id = str(seller_id).strip()
    reviews_snap = list(db.collection("seller_reviews").where("seller_id", "==", clean_id).stream())
    reviews: List[Dict[str, Any]] = []
    for doc in reviews_snap:
        d = doc.to_dict() or {}
        reviews.append(d | {"id": doc.id})

    reviews.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    count = len(reviews)
    avg = round(sum(float(r.get("rating") or 0) for r in reviews) / count, 1) if count else 0.0

    seller_name = ""
    for doc in db.collection("ads").limit(200).stream():
        d = doc.to_dict() or {}
        candidates = [
            str(d.get("seller_id") or ""),
            str(d.get("shop_id") or ""),
            str(d.get("author_id") or ""),
            str(d.get("owner_id") or ""),
            str(d.get("phone") or ""),
            str(doc.id),
        ]
        if clean_id in candidates:
            seller_name = str(d.get("shop_name") or d.get("seller_name") or d.get("author_name") or "").strip()
            if seller_name:
                break

    return {
        "seller_id": clean_id,
        "seller_name": seller_name or "Мастер / Продавец",
        "rating_avg": avg,
        "reviews_count": count,
        "reviews": reviews,
    }


@app.post("/api/sellers/{seller_id}/reviews")
def create_seller_review(
    seller_id: str,
    payload: ReviewCreate,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    clean_seller = str(seller_id).strip()
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Review text is required")

    author_name = (payload.author_name or "").strip()
    if not author_name and tg_user:
        author_name = " ".join(
            p for p in [tg_user.get("first_name"), tg_user.get("last_name")] if p
        ).strip() or str(tg_user.get("username") or "Покупатель")
    if not author_name:
        author_name = "Покупатель"

    review_doc = {
        "seller_id": clean_seller,
        "rating": max(1, min(5, int(payload.rating))),
        "text": text,
        "author_name": author_name,
        "author_id": tg_user.get("id") if tg_user else None,
        "created_at": iso_now(),
    }
    _, ref = db.collection("seller_reviews").add(review_doc)

    profile_data = get_seller_profile(clean_seller)
    return {
        "id": ref.id,
        "item": review_doc | {"id": ref.id},
        "rating_avg": profile_data["rating_avg"],
        "reviews_count": profile_data["reviews_count"],
        "reviews": profile_data["reviews"],
    }


# --- Shop Orders ---

@app.post("/api/orders")
def create_shop_order(
    order: ShopOrderCreate,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    if not order.items:
        raise HTTPException(status_code=400, detail="Cart is empty")

    buyer_name = (order.buyer_name or "").strip()
    buyer_phone = (order.buyer_phone or "").strip()
    if not buyer_name or not buyer_phone:
        raise HTTPException(status_code=400, detail="Buyer name and phone are required")

    first_item = order.items[0]
    shop_id = str(order.shop_id or first_item.shop_id or "").strip()
    shop_name = str(order.shop_name or first_item.shop_name or "Магазин").strip()
    owner_id = str(order.owner_id or first_item.owner_id or (shop_id if shop_id.isdigit() else "")).strip()
    owner_username = str(order.owner_username or first_item.owner_username or "").lstrip("@").strip()

    normalized_items: List[Dict[str, Any]] = []
    total_sum = 0.0

    for it in order.items:
        qty = max(1, int(it.qty))
        unit_price = float(it.unit_price) if it.unit_price is not None else parse_price_number(it.price)
        if unit_price <= 0:
            unit_price = parse_price_number(it.price)
        total_sum += unit_price * qty

        if it.ad_id:
            ad_ref = db.collection("ads").document(str(it.ad_id))
            ad_snap = ad_ref.get()
            if ad_snap.exists:
                ad_data = ad_snap.to_dict() or {}
                if not owner_id:
                    owner_id = str(ad_data.get("owner_id") or ad_data.get("seller_id") or ad_data.get("author_id") or "").strip()
                if not owner_username:
                    owner_username = str(ad_data.get("owner_username") or ad_data.get("seller_username") or ad_data.get("author_username") or "").lstrip("@").strip()
                if "stock" in ad_data and ad_data.get("stock") is not None:
                    current_stock = int(ad_data.get("stock") or 0)
                    if current_stock < qty:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Недостаточно товара «{it.title}» (в наличии: {current_stock} шт.)",
                        )
                    ad_ref.update({"stock": max(0, current_stock - qty), "updated_at": iso_now()})

        normalized_items.append(
            {
                "ad_id": it.ad_id,
                "shop_id": it.shop_id or shop_id,
                "shop_name": it.shop_name or shop_name,
                "title": it.title,
                "price": it.price or str(unit_price),
                "unit_price": unit_price,
                "category": it.category or "",
                "qty": qty,
            }
        )

    buyer_id = str(order.buyer_id or (tg_user.get("id") if tg_user else "") or "").strip()
    buyer_username = str(order.buyer_username or (tg_user.get("username") if tg_user else "") or "").lstrip("@").strip()

    order_doc = {
        "shop_id": shop_id,
        "shop_name": shop_name,
        "owner_id": owner_id,
        "owner_username": owner_username,
        "buyer_id": buyer_id,
        "buyer_username": buyer_username,
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "delivery_type": order.delivery_type or "Самовывоз",
        "delivery_address": (order.delivery_address or "").strip(),
        "comment": (order.comment or "").strip(),
        "items": normalized_items,
        "total_sum": round(total_sum, 2),
        "status": "new",
        "created_at": iso_now(),
    }

    _, ref = db.collection("shop_orders").add(order_doc)
    created = order_doc | {"id": ref.id}
    return {"id": ref.id, "status": "new", "item": created}


@app.get("/api/shop-orders")
def get_shop_orders(
    shop_id: Optional[str] = None,
    buyer_id: Optional[str] = None,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    tg_uid = str(tg_user.get("id") or "") if tg_user else ""
    ids = {x for x in [str(shop_id or "").strip(), str(buyer_id or "").strip(), tg_uid] if x}

    docs = [d.to_dict() | {"id": d.id} for d in db.collection("shop_orders").stream()]
    if ids:
        docs = [
            d
            for d in docs
            if str(d.get("shop_id") or "") in ids
            or str(d.get("buyer_id") or "") in ids
            or str(d.get("owner_id") or "") in ids
        ]
    docs.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"items": docs}


def _transition_order_status(order_id: str, new_status: str, restore_stock: bool = False):
    doc_ref = db.collection("shop_orders").document(order_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Order not found")

    data = snap.to_dict() or {}
    prev_status = data.get("status")

    if restore_stock and prev_status not in ("rejected", "cancelled"):
        for it in data.get("items") or []:
            ad_id = it.get("ad_id")
            qty = int(it.get("qty") or 0)
            if ad_id and qty > 0:
                ad_ref = db.collection("ads").document(str(ad_id))
                ad_snap = ad_ref.get()
                if ad_snap.exists:
                    ad_data = ad_snap.to_dict() or {}
                    if "stock" in ad_data and ad_data.get("stock") is not None:
                        cur_stock = int(ad_data.get("stock") or 0)
                        ad_ref.update({"stock": cur_stock + qty, "updated_at": iso_now()})

    doc_ref.update({"status": new_status, "updated_at": iso_now()})
    updated = doc_ref.get().to_dict() or {}
    return {"id": order_id, "status": new_status, "item": updated | {"id": order_id}}


@app.post("/api/orders/{order_id}/confirm")
def confirm_shop_order(order_id: str):
    return _transition_order_status(order_id, "confirmed", restore_stock=False)


@app.post("/api/orders/{order_id}/complete")
def complete_shop_order(order_id: str):
    return _transition_order_status(order_id, "completed", restore_stock=False)


@app.post("/api/orders/{order_id}/cancel")
def cancel_shop_order(order_id: str):
    return _transition_order_status(order_id, "rejected", restore_stock=True)


# --- Grooming Bookings ---

@app.get("/api/grooming/booked-slots")
def get_grooming_booked_slots(
    groomer_id: Optional[str] = None,
    ad_id: Optional[str] = None,
):
    docs = [d.to_dict() for d in db.collection("grooming_bookings").stream()]
    taken = []
    for d in docs:
        if d.get("status") in ("rejected", "cancelled"):
            continue
        if ad_id and d.get("ad_id") and str(d.get("ad_id")) == str(ad_id):
            taken.append({"date": d.get("date"), "time": d.get("time")})
        elif groomer_id and str(d.get("groomer_id") or "") == str(groomer_id):
            taken.append({"date": d.get("date"), "time": d.get("time")})
    return {"taken_slots": taken}


@app.post("/api/grooming/bookings")
def create_grooming_booking(
    booking: GroomingBookingCreate,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    date_str = (booking.date or "").strip()
    time_str = (booking.time or "").strip()
    pet_and_tasks = (booking.pet_and_tasks or "").strip()
    client_name = (booking.client_name or "").strip()
    client_phone = (booking.client_phone or "").strip()

    if not date_str or not time_str:
        raise HTTPException(status_code=400, detail="Date and time are required")
    if not pet_and_tasks or not client_name or not client_phone:
        raise HTTPException(status_code=400, detail="Pet description, name and phone are required")

    groomer_id = str(booking.groomer_id or booking.owner_id or "").strip()
    owner_id = str(booking.owner_id or groomer_id).strip()
    owner_username = str(booking.owner_username or booking.groomer_username or "").lstrip("@").strip()

    if booking.ad_id:
        ad_snap = db.collection("ads").document(str(booking.ad_id)).get()
        if ad_snap.exists:
            ad_data = ad_snap.to_dict() or {}
            if not owner_id:
                owner_id = str(ad_data.get("owner_id") or ad_data.get("seller_id") or ad_data.get("author_id") or "").strip()
            if not owner_username:
                owner_username = str(ad_data.get("owner_username") or ad_data.get("seller_username") or ad_data.get("author_username") or "").lstrip("@").strip()

    existing_docs = [d.to_dict() for d in db.collection("grooming_bookings").stream()]
    for ex in existing_docs:
        if ex.get("status") in ("rejected", "cancelled"):
            continue
        same_target = (booking.ad_id and str(ex.get("ad_id") or "") == str(booking.ad_id)) or (
            groomer_id and str(ex.get("groomer_id") or "") == groomer_id
        )
        if same_target and str(ex.get("date")) == date_str and str(ex.get("time")) == time_str:
            raise HTTPException(status_code=409, detail="Этот слот уже занят")

    client_id = str(booking.client_id or (tg_user.get("id") if tg_user else "") or "").strip()
    client_username = str(booking.client_username or (tg_user.get("username") if tg_user else "") or "").lstrip("@").strip()

    doc_data = {
        "ad_id": booking.ad_id,
        "groomer_id": groomer_id,
        "groomer_name": (booking.groomer_name or "Мастер").strip(),
        "groomer_phone": (booking.groomer_phone or "").strip(),
        "groomer_username": owner_username,
        "owner_id": owner_id,
        "owner_username": owner_username,
        "service_title": (booking.service_title or "Груминг").strip(),
        "date": date_str,
        "time": time_str,
        "pet_breed": (booking.pet_breed or "").strip(),
        "duration": (booking.duration or "90 мин").strip(),
        "pet_and_tasks": pet_and_tasks,
        "client_id": client_id,
        "client_username": client_username,
        "client_name": client_name,
        "client_phone": client_phone,
        "status": "pending",
        "created_at": iso_now(),
    }

    _, ref = db.collection("grooming_bookings").add(doc_data)
    created = doc_data | {"id": ref.id}
    return {"id": ref.id, "status": "pending", "item": created}


@app.get("/api/grooming/bookings")
def get_grooming_bookings(
    user_id: Optional[str] = None,
    groomer_id: Optional[str] = None,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    tg_uid = str(tg_user.get("id") or "") if tg_user else ""
    ids = {x for x in [str(user_id or "").strip(), str(groomer_id or "").strip(), tg_uid] if x}

    docs = [d.to_dict() | {"id": d.id} for d in db.collection("grooming_bookings").stream()]
    if ids:
        docs = [
            d
            for d in docs
            if str(d.get("groomer_id") or "") in ids
            or str(d.get("client_id") or "") in ids
            or str(d.get("owner_id") or "") in ids
        ]
    docs.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"items": docs}


@app.post("/api/grooming/bookings/{booking_id}/confirm")
def confirm_grooming_booking(booking_id: str):
    doc_ref = db.collection("grooming_bookings").document(booking_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Booking not found")
    doc_ref.update({"status": "confirmed", "updated_at": iso_now()})
    updated = doc_ref.get().to_dict() or {}
    return {"id": booking_id, "status": "confirmed", "item": updated | {"id": booking_id}}


@app.post("/api/grooming/bookings/{booking_id}/complete")
def complete_grooming_booking(booking_id: str):
    doc_ref = db.collection("grooming_bookings").document(booking_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Booking not found")
    doc_ref.update({"status": "completed", "updated_at": iso_now()})
    updated = doc_ref.get().to_dict() or {}
    return {"id": booking_id, "status": "completed", "item": updated | {"id": booking_id}}


@app.post("/api/grooming/bookings/{booking_id}/cancel")
def cancel_grooming_booking(booking_id: str, payload: GroomingCancelBody = Body(default=GroomingCancelBody())):
    doc_ref = db.collection("grooming_bookings").document(booking_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Booking not found")
    updates: Dict[str, Any] = {"status": "rejected", "updated_at": iso_now()}
    if payload.cancelled_by:
        updates["cancelled_by"] = payload.cancelled_by
    doc_ref.update(updates)
    updated = doc_ref.get().to_dict() or {}
    return {"id": booking_id, "status": "rejected", "item": updated | {"id": booking_id}}


# --- Vet Appointments ---

@app.get("/api/vet/booked-slots")
def get_vet_booked_slots(
    vet_id: Optional[str] = None,
    ad_id: Optional[str] = None,
):
    docs = [d.to_dict() for d in db.collection("vet_appointments").stream()]
    taken = []
    for d in docs:
        if d.get("status") in ("rejected", "cancelled"):
            continue
        if ad_id and d.get("ad_id") and str(d.get("ad_id")) == str(ad_id):
            taken.append({"date": d.get("date"), "time": d.get("time")})
        elif vet_id and str(d.get("vet_id") or "") == str(vet_id):
            taken.append({"date": d.get("date"), "time": d.get("time")})
    return {"taken_slots": taken}


@app.post("/api/vet/appointments")
def create_vet_appointment(
    appt: VetAppointmentCreate,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    date_str = (appt.date or "").strip()
    time_str = (appt.time or "").strip()
    pet_species = (appt.pet_species or "").strip()
    problem = (appt.problem or "").strip()
    client_name = (appt.client_name or "").strip()
    client_phone = (appt.client_phone or "").strip()

    if not date_str or not time_str:
        raise HTTPException(status_code=400, detail="Date and time are required")
    if not pet_species or not problem or not client_name or not client_phone:
        raise HTTPException(status_code=400, detail="Pet info, problem, name and phone are required")

    vet_id = str(appt.vet_id or appt.owner_id or "").strip()
    owner_id = str(appt.owner_id or vet_id).strip()
    owner_username = str(appt.owner_username or appt.vet_username or "").lstrip("@").strip()

    if appt.ad_id:
        ad_snap = db.collection("ads").document(str(appt.ad_id)).get()
        if ad_snap.exists:
            ad_data = ad_snap.to_dict() or {}
            if not owner_id:
                owner_id = str(ad_data.get("owner_id") or ad_data.get("seller_id") or ad_data.get("author_id") or "").strip()
            if not owner_username:
                owner_username = str(ad_data.get("owner_username") or ad_data.get("seller_username") or ad_data.get("author_username") or "").lstrip("@").strip()

    existing_docs = [d.to_dict() for d in db.collection("vet_appointments").stream()]
    for ex in existing_docs:
        if ex.get("status") in ("rejected", "cancelled"):
            continue
        same_target = (appt.ad_id and str(ex.get("ad_id") or "") == str(appt.ad_id)) or (
            vet_id and str(ex.get("vet_id") or "") == vet_id
        )
        if same_target and str(ex.get("date")) == date_str and str(ex.get("time")) == time_str:
            raise HTTPException(status_code=409, detail="Это время приёма уже занято")

    client_id = str(appt.client_id or (tg_user.get("id") if tg_user else "") or "").strip()
    client_username = str(appt.client_username or (tg_user.get("username") if tg_user else "") or "").lstrip("@").strip()

    doc_data = {
        "ad_id": appt.ad_id,
        "vet_id": vet_id,
        "vet_name": (appt.vet_name or "Ветеринар").strip(),
        "vet_phone": (appt.vet_phone or "").strip(),
        "vet_username": owner_username,
        "owner_id": owner_id,
        "owner_username": owner_username,
        "service_title": (appt.service_title or "Приём ветеринара").strip(),
        "date": date_str,
        "time": time_str,
        "pet_species": pet_species,
        "pet_weight": (appt.pet_weight or "").strip(),
        "problem": problem,
        "client_id": client_id,
        "client_username": client_username,
        "client_name": client_name,
        "client_phone": client_phone,
        "status": "pending",
        "created_at": iso_now(),
    }

    _, ref = db.collection("vet_appointments").add(doc_data)
    created = doc_data | {"id": ref.id}
    return {"id": ref.id, "status": "pending", "item": created}


@app.get("/api/vet/appointments")
def get_vet_appointments(
    user_id: Optional[str] = None,
    vet_id: Optional[str] = None,
    username: Optional[str] = None,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    tg_uid = str(tg_user.get("id") or "") if tg_user else ""
    tg_uname = str(tg_user.get("username") or "").lstrip("@").strip().lower() if tg_user else ""
    q_uname = str(username or "").lstrip("@").strip().lower()
    unames = {u for u in [tg_uname, q_uname] if u}
    ids = {x for x in [str(user_id or "").strip(), str(vet_id or "").strip(), tg_uid] if x}

    docs = [d.to_dict() | {"id": d.id} for d in db.collection("vet_appointments").stream()]
    if ids or unames:
        docs = [
            d
            for d in docs
            if str(d.get("vet_id") or "") in ids
            or str(d.get("client_id") or "") in ids
            or str(d.get("owner_id") or "") in ids
            or str(d.get("vet_username") or "").lstrip("@").strip().lower() in unames
            or str(d.get("owner_username") or "").lstrip("@").strip().lower() in unames
            or str(d.get("client_username") or "").lstrip("@").strip().lower() in unames
        ]
    docs.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"items": docs}


@app.post("/api/vet/appointments/{appointment_id}/status")
def update_vet_appointment_status(appointment_id: str, payload: StatusUpdateBody):
    new_status = (payload.status or "").strip()
    if new_status not in ("pending", "confirmed", "completed", "cancelled", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status")

    doc_ref = db.collection("vet_appointments").document(appointment_id)
    snap = doc_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Appointment not found")

    doc_ref.update({"status": new_status, "updated_at": iso_now()})
    updated = doc_ref.get().to_dict() or {}
    return {"id": appointment_id, "status": new_status, "item": updated | {"id": appointment_id}}


# --- Notifications ---

@app.post("/api/notifications/preferences")
def save_notification_preferences(
    payload: NotificationPreferencesBody,
    x_telegram_init_data: str = Header(default=""),
):
    tg_user = optional_tg_user(x_telegram_init_data)
    uid = str(payload.user_id or (tg_user.get("id") if tg_user else "") or "").strip()
    uname = str(payload.username or (tg_user.get("username") if tg_user else "") or "").lstrip("@").strip().lower()

    doc_id = f"uid_{uid}" if uid else (f"user_{uname}" if uname else "anonymous")
    doc_data = {
        "user_id": uid,
        "username": uname,
        "preferences": payload.preferences or {},
        "updated_at": iso_now(),
    }
    db.collection("notification_preferences").document(doc_id).set(doc_data, merge=True)
    return {"status": "ok", "id": doc_id}


@app.post("/api/notifications/dispatch")
def dispatch_notification(payload: NotificationDispatchBody):
    event = (payload.event or "").strip()
    data = payload.data or {}
    prefs = payload.preferences or {}

    if event == "ad_published" and not prefs.get("new_ads", True):
        return {"sent": False, "reason": "disabled_by_preference"}
    if event in ("order_created", "order_status", "grooming_created", "grooming_status", "vet_appointment_created", "vet_appointment_status") and not prefs.get("orders_and_grooming", True):
        return {"sent": False, "reason": "disabled_by_preference"}
    if event in ("low_stock", "ad_boosted") and not prefs.get("stock_and_boost", True):
        return {"sent": False, "reason": "disabled_by_preference"}

    target_chat = resolve_telegram_chat_id(
        payload.target_user_id or data.get("owner_id") or data.get("seller_id") or data.get("groomer_id") or data.get("vet_id") or data.get("buyer_id") or data.get("client_id"),
        payload.target_username or data.get("owner_username") or data.get("seller_username") or data.get("groomer_username") or data.get("vet_username") or data.get("buyer_username") or data.get("client_username"),
    )

    lines: List[str] = []
    if event == "ad_published":
        lines = [
            "📣 Новое объявление в ЗооМаркете!",
            f"Название: {data.get('title', '')}",
            f"Категория: {data.get('category', '')} · {data.get('price', '')}",
            f"Город: {data.get('city', 'не указан')}",
        ]
    elif event == "ad_boosted":
        lines = [
            "🚀 Объявление поднято в топ!",
            f"Объявление: {data.get('title', '')}",
            f"Срок закрепления: {data.get('hours', 24)} ч.",
        ]
    elif event == "low_stock":
        lines = [
            "⚠️ Малый остаток товара в магазине!",
            f"Товар: {data.get('title', '')}",
            f"Осталось: {data.get('stock', 0)} шт.",
        ]
    elif event == "order_created":
        items_str = ", ".join(
            f"{it.get('title', '')} × {it.get('qty', 1)}" for it in (data.get("items") or [])
        )
        lines = [
            "🛒 Новый заказ в магазине ЗооМаркет!",
            f"Покупатель: {data.get('buyer_name', '')} ({data.get('buyer_phone', '')})",
            f"Получение: {data.get('delivery_type', 'Самовывоз')}",
            f"Состав: {items_str}",
            f"Итого: {data.get('total_sum', 0)}",
        ]
    elif event == "order_status":
        lines = [
            "📦 Обновлён статус заказа в ЗооМаркете",
            f"Заказ: #{str(data.get('order_id', ''))[:8]}",
            f"Статус: {data.get('status', '')}",
            f"Сумма: {data.get('total_sum', 0)}",
        ]
    elif event == "grooming_created":
        lines = [
            "✂️ Новая заявка на груминг!",
            f"Дата и время: {data.get('date', '')} в {data.get('time', '')}",
            f"Клиент: {data.get('client_name', '')} ({data.get('client_phone', '')})",
            f"Питомец и задачи: {data.get('pet_and_tasks', '')}",
        ]
    elif event == "grooming_status":
        lines = [
            "✂️ Обновлён статус записи на груминг",
            f"Мастер: {data.get('groomer_name', 'Мастер')}",
            f"Дата и время: {data.get('date', '')} в {data.get('time', '')}",
            f"Статус: {data.get('status', '')}",
        ]
    elif event == "vet_appointment_created":
        lines = [
            "🩺 Новая запись на приём к ветеринару!",
            f"Дата и время: {data.get('date', '')} в {data.get('time', '')}",
            f"Врач: {data.get('vet_name', 'Ветеринар')}",
            f"Клиент: {data.get('client_name', '')} ({data.get('client_phone', '')})",
            f"Питомец: {data.get('pet_species', '')}",
            f"Проблема: {data.get('problem', '')}",
        ]
    elif event == "vet_appointment_status":
        lines = [
            "🩺 Обновлён статус приёма у ветеринара",
            f"Врач: {data.get('vet_name', 'Ветеринар')}",
            f"Дата и время: {data.get('date', '')} в {data.get('time', '')}",
            f"Статус: {data.get('status', '')}",
        ]

    message_text = "\n".join(line for line in lines if line.strip())
    sent = False
    if target_chat and message_text:
        sent = send_telegram_message(target_chat, message_text)

    return {
        "ok": True,
        "sent": sent,
        "target_chat": target_chat,
        "event": event,
    }
