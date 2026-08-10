import os
import uuid

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

CID = os.environ.get("KAKAOPAY_CID", "TC0ONETIME")
SECRET_KEY = os.environ.get("KAKAOPAY_SECRET_KEY")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")
KAKAO_API = "https://open-api.kakaopay.com/online/v1/payment"

# 결제 준비(ready)~승인(approve) 사이 tid를 보관하기 위한 임시 저장소.
# 카카오 정책상 결제 준비 후 15분 내 승인되지 않으면 자동 만료된다.
orders = {}


def kakao_headers():
    return {
        "Authorization": f"SECRET_KEY {SECRET_KEY}",
        "Content-Type": "application/json",
    }


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
    return redirect(f"/payment-result.html?status=success&order={partner_order_id}&amount={order['total_amount']}")


@app.route("/api/orders/<order_id>")
def get_order(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "not found"}), 404
    return jsonify(order)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
