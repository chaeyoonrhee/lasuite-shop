import base64
import json
import os
import uuid

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
GITHUB_API = "https://api.github.com"

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


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/payment/ready", methods=["POST"])
def payment_ready():
    if not SECRET_KEY or not SECRET_KEY.isascii():
        return jsonify({"error": "KAKAOPAY_SECRET_KEY가 설정되지 않았습니다. .env 파일에 카카오 개발자센터에서 발급받은 시크릿 키를 넣어주세요."}), 500

    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    partner_user_id = data.get("partnerUserId") or "guest"

    if not items:
        return jsonify({"error": "장바구니가 비어 있습니다."}), 400

    try:
        total_amount = sum(int(i["price"]) * int(i["qty"]) for i in items)
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
    record_order({
        "partner_order_id": partner_order_id,
        "items": order["items"],
        "total_amount": order["total_amount"],
        "partner_user_id": order["partner_user_id"],
        "approved_at": order["approved_at"],
    })
    return redirect(f"/payment-result.html?status=success&order={partner_order_id}&amount={order['total_amount']}")


@app.route("/api/orders/<order_id>")
def get_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "not found"}), 404
    return jsonify(order)


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
</style></head><body>
  <div class="wrap">
    <div class="head">
      <div class="logo">LA SUITE 주문 내역</div>
      <a class="logout" href="/admin/logout">로그아웃</a>
    </div>
    {% if orders %}
      <div class="summary">총 {{ orders|length }}건 · 합계 {{ '{:,}'.format(total) }}원</div>
      <table>
        <tr><th>주문시각</th><th>주문번호</th><th>상품</th><th>금액</th></tr>
        {% for o in orders %}
        <tr>
          <td>{{ o.approved_at or '-' }}</td>
          <td>{{ o.partner_order_id[:8] }}</td>
          <td>{% for it in o['items'] %}{{ it.kr }}({{ it.size }}) x{{ it.qty }}{% if not loop.last %}, {% endif %}{% endfor %}</td>
          <td class="amt">{{ '{:,}'.format(o.total_amount) }}원</td>
        </tr>
        {% endfor %}
      </table>
    {% else %}
      <div class="empty">아직 들어온 주문이 없습니다.</div>
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


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect("/admin")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
