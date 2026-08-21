import base64
import json
import os
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory, session

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

CID = os.environ.get("KAKAOPAY_CID", "TC0ONETIME")
SECRET_KEY = os.environ.get("KAKAOPAY_SECRET_KEY")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
KAKAO_API = "https://open-api.kakaopay.com/online/v1/payment"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "chaeyoonrhee/lasuite-shop")
GITHUB_ORDERS_PATH = os.environ.get("GITHUB_ORDERS_PATH", "orders/orders.json")
GITHUB_PURCHASES_PATH = os.environ.get("GITHUB_PURCHASES_PATH", "data/purchases.json")
GITHUB_API = "https://api.github.com"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 결제 준비(ready)~승인(approve) 사이 tid를 보관하기 위한 임시 저장소.
# 카카오 정책상 결제 준비 후 15분 내 승인되지 않으면 자동 만료된다.
orders = {}


def kakao_headers():
    return {
        "Authorization": f"SECRET_KEY {SECRET_KEY}",
        "Content-Type": "application/json",
    }


def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def github_get_orders():
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_ORDERS_PATH}"
    r = requests.get(url, headers=github_headers(), timeout=10)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]


def github_save_orders(orders_list, sha, message):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_ORDERS_PATH}"
    content_b64 = base64.b64encode(
        json.dumps(orders_list, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    body = {"message": message, "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(url, json=body, headers=github_headers(), timeout=10)
    r.raise_for_status()


def github_get_purchases():
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_PURCHASES_PATH}"
    r = requests.get(url, headers=github_headers(), timeout=10)
    if r.status_code == 404:
        return [], None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]


def github_save_purchases(purchases_list, sha, message):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_PURCHASES_PATH}"
    content_b64 = base64.b64encode(
        json.dumps(purchases_list, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    body = {"message": message, "content": content_b64, "branch": "main"}
    if sha:
        body["sha"] = sha
    r = requests.put(url, json=body, headers=github_headers(), timeout=10)
    r.raise_for_status()


def record_order(order_record):
    """주문 확정 내역을 GitHub 저장소에 영구 기록한다. 실패해도 결제 흐름은 막지 않는다."""
    if not GITHUB_TOKEN:
        return
    try:
        orders_list, sha = github_get_orders()
        orders_list.append(order_record)
        github_save_orders(orders_list, sha, f"Order {order_record['partner_order_id']}")
    except Exception:
        pass


def github_get_json_file(path, default):
    """GitHub 저장소의 JSON 파일을 실시간으로 읽어온다. 배포와 무관하게 항상 최신 내용을 반환한다."""
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path}"
    r = requests.get(url, headers=github_headers(), timeout=10)
    if r.status_code == 404:
        return default, None
    r.raise_for_status()
    data = r.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]


def notify_telegram(order_record):
    """새 주문 알림을 텔레그램으로 보낸다. 실패해도 주문 흐름은 막지 않는다."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        items_text = ", ".join(
            f'{i["kr"]}({i.get("size", "-")}) x{i["qty"]}' for i in order_record["items"]
        )
        method = "카카오페이 결제완료" if order_record.get("payment_method") == "kakaopay" else "무통장입금 (입금대기)"
        lines = [
            "🛍 라스윗 새 주문",
            f'상품: {items_text}',
            f'금액: {order_record["total_amount"]:,}원',
            f'결제수단: {method}',
            f'주문자: {order_record.get("orderer_name", "-")} / {order_record.get("orderer_phone", "-")}',
            f'받는분: {order_record.get("shipping_name", "-")} / {order_record.get("shipping_phone", "-")}',
            f'주소: {order_record.get("shipping_address", "-")}',
        ]
        if order_record.get("depositor_name"):
            lines.append(f'입금자명: {order_record["depositor_name"]}')
        text = "\n".join(lines)
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:
        pass


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


GITHUB_PRODUCTS_PATH = os.environ.get("GITHUB_PRODUCTS_PATH", "data/products.json")


@app.route("/api/products")
def api_products():
    """상품 목록을 GitHub 저장소에서 실시간으로 읽어온다.
    products.json만 GitHub에서 수정하면 재배포 없이 바로 사이트에 반영된다."""
    if not GITHUB_TOKEN:
        return jsonify([])
    try:
        products, _ = github_get_json_file(GITHUB_PRODUCTS_PATH, [])
    except Exception:
        products = []
    return jsonify([p for p in products if not p.get("hidden")])


@app.route("/api/payment/ready", methods=["POST"])
def payment_ready():
    if not SECRET_KEY or not SECRET_KEY.isascii():
        return jsonify({"error": "KAKAOPAY_SECRET_KEY가 설정되지 않았습니다. .env 파일에 카카오 개발자센터에서 발급받은 시크릿 키를 넣어주세요."}), 500

    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    partner_user_id = data.get("partnerUserId") or "guest"
    orderer = data.get("orderer") or {}
    orderer_name = (orderer.get("name") or "").strip()
    orderer_phone = (orderer.get("phone") or "").strip()
    shipping = data.get("shipping") or {}
    shipping_name = (shipping.get("name") or "").strip()
    shipping_phone = (shipping.get("phone") or "").strip()
    shipping_address = (shipping.get("address") or "").strip()

    if not items:
        return jsonify({"error": "장바구니가 비어 있습니다."}), 400

    if not orderer_name or not orderer_phone:
        return jsonify({"error": "주문자 성함과 연락처를 입력해 주세요."}), 400

    if not shipping_name or not shipping_phone or not shipping_address:
        return jsonify({"error": "받는 분 성함, 연락처, 배송지 주소를 모두 입력해 주세요."}), 400

    try:
        shipping_fee = int(data.get("shippingFee") or 0)
        total_amount = sum(int(i["price"]) * int(i["qty"]) for i in items) + shipping_fee
        quantity = sum(int(i["qty"]) for i in items)
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "장바구니 항목 형식이 올바르지 않습니다."}), 400

    if total_amount <= 0:
        return jsonify({"error": "결제 금액이 올바르지 않습니다."}), 400

    item_name = items[0]["kr"] if len(items) == 1 else f'{items[0]["kr"]} 외 {len(items) - 1}건'
    partner_order_id = uuid.uuid4().hex

    body = {
        "cid": CID,
        "partner_order_id": partner_order_id,
        "partner_user_id": partner_user_id,
        "item_name": item_name,
        "quantity": quantity,
        "total_amount": total_amount,
        "tax_free_amount": 0,
        "approval_url": f"{BASE_URL}/api/payment/approve?partner_order_id={partner_order_id}",
        "cancel_url": f"{BASE_URL}/payment-result.html?status=cancel",
        "fail_url": f"{BASE_URL}/payment-result.html?status=fail",
    }

    try:
        r = requests.post(f"{KAKAO_API}/ready", json=body, headers=kakao_headers(), timeout=10)
        result = r.json()
    except Exception:
        return jsonify({"error": "카카오페이 서버와 통신 중 오류가 발생했습니다. KAKAOPAY_SECRET_KEY 값이 올바른지 확인해 주세요."}), 502

    if r.status_code != 200:
        return jsonify({"error": result.get("msg", "카카오페이 결제 준비에 실패했습니다."), "detail": result}), r.status_code

    orders[partner_order_id] = {
        "tid": result["tid"],
        "items": items,
        "total_amount": total_amount,
        "shipping_fee": shipping_fee,
        "orderer_name": orderer_name,
        "orderer_phone": orderer_phone,
        "shipping_name": shipping_name,
        "shipping_phone": shipping_phone,
        "shipping_address": shipping_address,
        "partner_user_id": partner_user_id,
        "status": "READY",
    }

    return jsonify({
        "redirectUrl": result.get("next_redirect_pc_url"),
        "mobileRedirectUrl": result.get("next_redirect_mobile_url"),
    })


@app.route("/api/payment/approve")
def payment_approve():
    partner_order_id = request.args.get("partner_order_id")
    pg_token = request.args.get("pg_token")
    order = orders.get(partner_order_id)

    if not order or not pg_token:
        return redirect("/payment-result.html?status=fail")

    body = {
        "cid": CID,
        "tid": order["tid"],
        "partner_order_id": partner_order_id,
        "partner_user_id": order["partner_user_id"],
        "pg_token": pg_token,
    }

    try:
        r = requests.post(f"{KAKAO_API}/approve", json=body, headers=kakao_headers(), timeout=10)
        result = r.json()
    except Exception:
        order["status"] = "FAILED"
        return redirect("/payment-result.html?status=fail")

    if r.status_code != 200:
        order["status"] = "FAILED"
        return redirect("/payment-result.html?status=fail")

    order["status"] = "APPROVED"
    order["approved_at"] = result.get("approved_at")
    order_record = {
        "partner_order_id": partner_order_id,
        "items": order["items"],
        "total_amount": order["total_amount"],
        "shipping_fee": order.get("shipping_fee", 0),
        "orderer_name": order.get("orderer_name", ""),
        "orderer_phone": order.get("orderer_phone", ""),
        "shipping_name": order.get("shipping_name", ""),
        "shipping_phone": order.get("shipping_phone", ""),
        "shipping_address": order.get("shipping_address", ""),
        "partner_user_id": order["partner_user_id"],
        "payment_method": "kakaopay",
        "status": "결제완료",
        "approved_at": order["approved_at"],
    }
    record_order(order_record)
    notify_telegram(order_record)
    return redirect(f"/payment-result.html?status=success&order={partner_order_id}&amount={order['total_amount']}")


@app.route("/api/orders/<order_id>")
def get_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "not found"}), 404
    return jsonify(order)


@app.route("/api/orders/bank-transfer", methods=["POST"])
def bank_transfer_order():
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    partner_user_id = data.get("partnerUserId") or "guest"
    orderer = data.get("orderer") or {}
    orderer_name = (orderer.get("name") or "").strip()
    orderer_phone = (orderer.get("phone") or "").strip()
    shipping = data.get("shipping") or {}
    shipping_name = (shipping.get("name") or "").strip()
    shipping_phone = (shipping.get("phone") or "").strip()
    shipping_address = (shipping.get("address") or "").strip()
    depositor_name = (data.get("depositorName") or "").strip()

    if not items:
        return jsonify({"error": "장바구니가 비어 있습니다."}), 400
    if not orderer_name or not orderer_phone:
        return jsonify({"error": "주문자 성함과 연락처를 입력해 주세요."}), 400
    if not shipping_name or not shipping_phone or not shipping_address:
        return jsonify({"error": "받는 분 성함, 연락처, 배송지 주소를 모두 입력해 주세요."}), 400
    if not depositor_name:
        return jsonify({"error": "입금자명을 입력해 주세요."}), 400

    try:
        shipping_fee = int(data.get("shippingFee") or 0)
        total_amount = sum(int(i["price"]) * int(i["qty"]) for i in items) + shipping_fee
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "장바구니 항목 형식이 올바르지 않습니다."}), 400

    if total_amount <= 0:
        return jsonify({"error": "결제 금액이 올바르지 않습니다."}), 400

    order_id = uuid.uuid4().hex
    order_record = {
        "partner_order_id": order_id,
        "items": items,
        "total_amount": total_amount,
        "shipping_fee": shipping_fee,
        "orderer_name": orderer_name,
        "orderer_phone": orderer_phone,
        "shipping_name": shipping_name,
        "shipping_phone": shipping_phone,
        "shipping_address": shipping_address,
        "partner_user_id": partner_user_id,
        "payment_method": "bank_transfer",
        "depositor_name": depositor_name,
        "status": "입금대기",
        "approved_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    record_order(order_record)
    notify_telegram(order_record)

    return jsonify({"orderId": order_id, "totalAmount": total_amount})


def _normalize_phone(phone):
    return "".join(ch for ch in phone if ch.isdigit())


@app.route("/api/orders/lookup", methods=["POST"])
def lookup_orders():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name or not phone:
        return jsonify({"error": "성함과 연락처를 모두 입력해 주세요."}), 400

    if not GITHUB_TOKEN:
        return jsonify({"orders": []})

    try:
        orders_list, _ = github_get_orders()
    except Exception:
        return jsonify({"error": "조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 502

    phone_norm = _normalize_phone(phone)
    matched = [
        o for o in orders_list
        if (o.get("orderer_name") or o.get("shipping_name", "")) == name
        and _normalize_phone(o.get("orderer_phone") or o.get("shipping_phone", "")) == phone_norm
    ]
    matched.sort(key=lambda o: o.get("approved_at") or "", reverse=True)

    result = [
        {
            "orderId": o["partner_order_id"][:8],
            "date": (o.get("approved_at") or "")[:16].replace("T", " "),
            "items": [
                {"kr": i.get("kr"), "size": i.get("size"), "qty": i.get("qty")}
                for i in o.get("items", [])
            ],
            "totalAmount": o.get("total_amount", 0),
            "paymentMethod": "카카오페이" if o.get("payment_method") == "kakaopay" else "무통장입금",
            "status": o.get("status") or "결제완료",
            "courier": "GS25 편의점택배" if o.get("tracking_number") else None,
            "trackingNumber": o.get("tracking_number") or None,
        }
        for o in matched
    ]
    return jsonify({"orders": result})


ADMIN_LOGIN_HTML = """
<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<title>LA SUITE — 관리자</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Pretendard:wght@400;500&display=swap" rel="stylesheet">
<style>
  body{font-family:'Pretendard',sans-serif;background:#f7f4ef;color:#2b2620;min-height:100vh;display:flex;align-items:center;justify-content:center;margin:0;}
  .box{background:#fff;border:1px solid #e2dbcd;padding:48px 40px;width:100%;max-width:340px;}
  .logo{font-family:'Cormorant Garamond',serif;font-size:22px;letter-spacing:0.25em;text-align:center;margin-bottom:28px;}
  input{width:100%;padding:13px;border:1px solid #e2dbcd;font-size:14px;box-sizing:border-box;margin-bottom:14px;font-family:inherit;}
  button{width:100%;padding:13px;background:#2b2620;color:#f7f4ef;border:none;font-size:13px;letter-spacing:0.05em;cursor:pointer;}
  .err{color:#b5624a;font-size:12.5px;margin-bottom:14px;text-align:center;}
</style></head><body>
  <div class="box">
    <div class="logo">LA SUITE</div>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post">
      <input type="password" name="password" placeholder="관리자 비밀번호" autofocus>
      <button type="submit">로그인</button>
    </form>
  </div>
</body></html>
"""

ADMIN_ORDERS_HTML = """
<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<title>LA SUITE — 주문 내역</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Pretendard:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  body{font-family:'Pretendard',sans-serif;background:#f7f4ef;color:#2b2620;margin:0;padding:40px 24px;}
  .wrap{max-width:960px;margin:0 auto;}
  .head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:28px;flex-wrap:wrap;gap:12px;}
  .logo{font-family:'Cormorant Garamond',serif;font-size:24px;letter-spacing:0.2em;}
  a.logout{font-size:12.5px;color:#6b6459;text-decoration:none;border-bottom:1px solid #e2dbcd;}
  table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2dbcd;}
  th,td{text-align:left;padding:14px 16px;font-size:13px;border-bottom:1px solid #e2dbcd;}
  th{font-weight:500;color:#6b6459;font-size:11.5px;letter-spacing:0.05em;text-transform:uppercase;background:#efe9df;}
  tr:last-child td{border-bottom:none;}
  .amt{font-weight:600;}
  .empty{padding:80px 20px;text-align:center;color:#6b6459;font-size:14px;background:#fff;border:1px solid #e2dbcd;}
  .summary{font-size:13px;color:#6b6459;margin-bottom:14px;}
  .tag{display:inline-block;padding:3px 8px;font-size:11px;border-radius:3px;}
  .tag.paid{background:#eef3ea;color:#5c8a52;}
  .tag.pending{background:#f5eee0;color:#a9895f;}
  .confirm-btn{margin-top:6px;padding:5px 10px;font-size:11px;background:#2b2620;color:#f7f4ef;border:none;cursor:pointer;}
  .tracking-form{display:flex;gap:4px;margin-top:4px;}
  .tracking-form input{width:110px;padding:5px 6px;font-size:11px;border:1px solid #e2dbcd;font-family:inherit;}
  .tracking-form button{padding:5px 8px;font-size:11px;background:#2b2620;color:#f7f4ef;border:none;cursor:pointer;}
</style></head><body>
  <div class="wrap">
    <div class="head">
      <div class="logo">LA SUITE 주문 내역</div>
      <div style="display:flex;gap:16px;align-items:baseline;">
        <a class="logout" href="/admin/purchases">매입/수익</a>
        <a class="logout" href="/admin/logout">로그아웃</a>
      </div>
    </div>
    {% if orders %}
      <div class="summary">총 {{ orders|length }}건 · 합계 {{ '{:,}'.format(total) }}원</div>
      <table>
        <tr><th>주문시각</th><th>주문번호</th><th>상품</th><th>주문자</th><th>받는분</th><th>연락처</th><th>배송지</th><th>결제수단</th><th>상태</th><th>운송장번호(GS25)</th><th>금액</th><th>처리</th></tr>
        {% for o in orders %}
        <tr{% if o.fulfilled %} style="opacity:0.55;"{% endif %}>
          <td>{{ o.approved_at or '-' }}</td>
          <td>{{ o.partner_order_id[:8] }}</td>
          <td>{% for it in o['items'] %}{{ it.kr }}({{ it.size }}) x{{ it.qty }}{% if not loop.last %}, {% endif %}{% endfor %}</td>
          <td>{{ o.orderer_name or o.shipping_name or '-' }}<br><span style="color:#6b6459;font-size:11px;">{{ o.orderer_phone or o.shipping_phone or '-' }}</span></td>
          <td>{{ o.shipping_name or '-' }}</td>
          <td>{{ o.shipping_phone or '-' }}</td>
          <td>{{ o.shipping_address or '-' }}</td>
          <td>{{ '카카오페이' if o.payment_method == 'kakaopay' else '무통장입금' }}{% if o.depositor_name %}<br><span style="color:#6b6459;font-size:11px;">입금자 {{ o.depositor_name }}</span>{% endif %}</td>
          <td>
            {% if o.status == '입금대기' %}
              <span class="tag pending">입금대기</span>
              <form method="post" action="/admin/orders/{{ o.partner_order_id }}/confirm">
                <button type="submit" class="confirm-btn">입금확인</button>
              </form>
            {% else %}
              <span class="tag paid">{{ o.status or '결제완료' }}</span>
            {% endif %}
          </td>
          <td>
            <form method="post" action="/admin/orders/{{ o.partner_order_id }}/tracking" class="tracking-form">
              <input type="text" name="tracking_number" placeholder="운송장번호" value="{{ o.tracking_number or '' }}">
              <button type="submit">저장</button>
            </form>
          </td>
          <td class="amt">{{ '{:,}'.format(o.total_amount) }}원{% if o.shipping_fee %}<br><span style="font-weight:400;color:#6b6459;font-size:11px;">(배송비 {{ '{:,}'.format(o.shipping_fee) }}원 포함)</span>{% endif %}</td>
          <td>
            <form method="post" action="/admin/orders/{{ o.partner_order_id }}/fulfill">
              <button type="submit" class="confirm-btn" style="{% if o.fulfilled %}background:#5c8a52;{% endif %}">{{ '처리완료 ✓' if o.fulfilled else '처리완료로 표시' }}</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">아직 들어온 주문이 없습니다.</div>
    {% endif %}
  </div>
</body></html>
"""

ADMIN_PURCHASES_HTML = """
<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<title>LA SUITE — 매입/수익</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Pretendard:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  body{font-family:'Pretendard',sans-serif;background:#f7f4ef;color:#2b2620;margin:0;padding:40px 24px;}
  .wrap{max-width:820px;margin:0 auto;}
  .head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:28px;flex-wrap:wrap;gap:12px;}
  .logo{font-family:'Cormorant Garamond',serif;font-size:24px;letter-spacing:0.2em;}
  a.logout{font-size:12.5px;color:#6b6459;text-decoration:none;border-bottom:1px solid #e2dbcd;}
  table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2dbcd;margin-bottom:36px;}
  th,td{text-align:left;padding:14px 16px;font-size:13px;border-bottom:1px solid #e2dbcd;}
  th{font-weight:500;color:#6b6459;font-size:11.5px;letter-spacing:0.05em;text-transform:uppercase;background:#efe9df;}
  tr:last-child td{border-bottom:none;}
  .amt{font-weight:600;}
  .profit-pos{color:#5c8a52;}
  .profit-neg{color:#b5624a;}
  .empty{padding:60px 20px;text-align:center;color:#6b6459;font-size:14px;background:#fff;border:1px solid #e2dbcd;margin-bottom:36px;}
  .section-title{font-family:'Cormorant Garamond',serif;font-size:19px;margin-bottom:14px;}
  .add-form{background:#fff;border:1px solid #e2dbcd;padding:20px;margin-bottom:36px;display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;}
  .add-form label{display:block;font-size:11px;color:#6b6459;margin-bottom:5px;}
  .add-form input{padding:9px 10px;border:1px solid #e2dbcd;font-size:13px;font-family:inherit;}
  .add-form input[name="memo"]{width:220px;}
  .add-form button{padding:10px 18px;background:#2b2620;color:#f7f4ef;border:none;font-size:12.5px;cursor:pointer;}
  .del-btn{background:none;border:none;color:#b5624a;font-size:11px;cursor:pointer;text-decoration:underline;padding:0;}
</style></head><body>
  <div class="wrap">
    <div class="head">
      <div class="logo">LA SUITE 매입/수익</div>
      <div style="display:flex;gap:16px;align-items:baseline;">
        <a class="logout" href="/admin/orders">주문 내역</a>
        <a class="logout" href="/admin/logout">로그아웃</a>
      </div>
    </div>

    <div class="section-title">월별 손익</div>
    {% if monthly %}
      <table>
        <tr><th>월</th><th>매출</th><th>매입</th><th>수익</th></tr>
        {% for m in monthly %}
        <tr>
          <td>{{ m.month }}</td>
          <td class="amt">{{ '{:,}'.format(m.revenue) }}원</td>
          <td class="amt">{{ '{:,}'.format(m.cost) }}원</td>
          <td class="amt {{ 'profit-pos' if m.profit >= 0 else 'profit-neg' }}">{{ '{:,}'.format(m.profit) }}원</td>
        </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">아직 데이터가 없습니다.</div>
    {% endif %}

    <div class="section-title">매입 등록</div>
    <form method="post" action="/admin/purchases/add" class="add-form">
      <div>
        <label>날짜</label>
        <input type="date" name="date" value="{{ today }}" required>
      </div>
      <div>
        <label>금액</label>
        <input type="number" name="amount" placeholder="예: 210000" required>
      </div>
      <div>
        <label>메모</label>
        <input type="text" name="memo" placeholder="예: 쫀득 핀터플레어 바지 사입">
      </div>
      <button type="submit">등록</button>
    </form>

    <div class="section-title">매입 내역</div>
    {% if purchases %}
      <table>
        <tr><th>날짜</th><th>금액</th><th>메모</th><th></th></tr>
        {% for p in purchases %}
        <tr>
          <td>{{ p.date }}</td>
          <td class="amt">{{ '{:,}'.format(p.amount) }}원</td>
          <td>{{ p.memo or '-' }}</td>
          <td>
            <form method="post" action="/admin/purchases/{{ p.id }}/delete" onsubmit="return confirm('삭제할까요?');">
              <button type="submit" class="del-btn">삭제</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">등록된 매입 내역이 없습니다.</div>
    {% endif %}
  </div>
</body></html>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect("/admin/orders")
        return render_template_string(ADMIN_LOGIN_HTML, error="비밀번호가 올바르지 않습니다."), 401
    if session.get("is_admin"):
        return redirect("/admin/orders")
    return render_template_string(ADMIN_LOGIN_HTML, error=None)


@app.route("/admin/orders")
def admin_orders():
    if not session.get("is_admin"):
        return redirect("/admin")
    if not GITHUB_TOKEN:
        return render_template_string(ADMIN_ORDERS_HTML, orders=[], total=0)
    orders_list, _ = github_get_orders()
    orders_list = sorted(orders_list, key=lambda o: o.get("approved_at") or "", reverse=True)
    total = sum(o.get("total_amount", 0) for o in orders_list)
    return render_template_string(ADMIN_ORDERS_HTML, orders=orders_list, total=total)


@app.route("/admin/orders/<order_id>/confirm", methods=["POST"])
def admin_confirm_order(order_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if GITHUB_TOKEN:
        try:
            orders_list, sha = github_get_orders()
            for o in orders_list:
                if o.get("partner_order_id") == order_id:
                    o["status"] = "입금확인"
                    break
            github_save_orders(orders_list, sha, f"Confirm deposit {order_id}")
        except Exception:
            pass
    return redirect("/admin/orders")


@app.route("/admin/orders/<order_id>/fulfill", methods=["POST"])
def admin_fulfill_order(order_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if GITHUB_TOKEN:
        try:
            orders_list, sha = github_get_orders()
            for o in orders_list:
                if o.get("partner_order_id") == order_id:
                    o["fulfilled"] = not o.get("fulfilled", False)
                    break
            github_save_orders(orders_list, sha, f"Toggle fulfilled {order_id}")
        except Exception:
            pass
    return redirect("/admin/orders")


@app.route("/admin/orders/<order_id>/tracking", methods=["POST"])
def admin_set_tracking(order_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    tracking_number = (request.form.get("tracking_number") or "").strip()
    if GITHUB_TOKEN:
        try:
            orders_list, sha = github_get_orders()
            for o in orders_list:
                if o.get("partner_order_id") == order_id:
                    o["tracking_number"] = tracking_number
                    break
            github_save_orders(orders_list, sha, f"Set tracking number {order_id}")
        except Exception:
            pass
    return redirect("/admin/orders")


@app.route("/admin/purchases")
def admin_purchases():
    if not session.get("is_admin"):
        return redirect("/admin")
    if not GITHUB_TOKEN:
        return render_template_string(ADMIN_PURCHASES_HTML, purchases=[], monthly=[], today=datetime.utcnow().strftime("%Y-%m-%d"))

    try:
        purchases_list, _ = github_get_purchases()
    except Exception:
        purchases_list = []
    try:
        orders_list, _ = github_get_orders()
    except Exception:
        orders_list = []

    purchases_list = sorted(purchases_list, key=lambda p: p.get("date") or "", reverse=True)

    revenue_by_month = {}
    for o in orders_list:
        if o.get("status") not in ("결제완료", "입금확인"):
            continue
        month = (o.get("approved_at") or "")[:7]
        if not month:
            continue
        revenue_by_month[month] = revenue_by_month.get(month, 0) + (o.get("total_amount") or 0)

    cost_by_month = {}
    for p in purchases_list:
        month = (p.get("date") or "")[:7]
        if not month:
            continue
        cost_by_month[month] = cost_by_month.get(month, 0) + (p.get("amount") or 0)

    months = sorted(set(revenue_by_month) | set(cost_by_month), reverse=True)
    monthly = [
        {
            "month": m,
            "revenue": revenue_by_month.get(m, 0),
            "cost": cost_by_month.get(m, 0),
            "profit": revenue_by_month.get(m, 0) - cost_by_month.get(m, 0),
        }
        for m in months
    ]

    return render_template_string(
        ADMIN_PURCHASES_HTML,
        purchases=purchases_list,
        monthly=monthly,
        today=datetime.utcnow().strftime("%Y-%m-%d"),
    )


@app.route("/admin/purchases/add", methods=["POST"])
def admin_add_purchase():
    if not session.get("is_admin"):
        return redirect("/admin")
    date = (request.form.get("date") or "").strip()
    memo = (request.form.get("memo") or "").strip()
    try:
        amount = int(request.form.get("amount") or 0)
    except ValueError:
        amount = 0
    if date and amount and GITHUB_TOKEN:
        try:
            purchases_list, sha = github_get_purchases()
            purchases_list.append({
                "id": uuid.uuid4().hex[:8],
                "date": date,
                "amount": amount,
                "memo": memo,
                "created_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
            github_save_purchases(purchases_list, sha, f"Add purchase {date} {amount}")
        except Exception:
            pass
    return redirect("/admin/purchases")


@app.route("/admin/purchases/<purchase_id>/delete", methods=["POST"])
def admin_delete_purchase(purchase_id):
    if not session.get("is_admin"):
        return redirect("/admin")
    if GITHUB_TOKEN:
        try:
            purchases_list, sha = github_get_purchases()
            purchases_list = [p for p in purchases_list if p.get("id") != purchase_id]
            github_save_purchases(purchases_list, sha, f"Delete purchase {purchase_id}")
        except Exception:
            pass
    return redirect("/admin/purchases")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect("/admin")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
