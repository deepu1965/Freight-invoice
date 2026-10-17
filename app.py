"""Freight-Invoice Reconciliation Agent  (hackathon prototype)
Upload invoice -> Sarvam Vision extracts fields -> reconcile vs PO -> dispute email
in the supplier's own language.
"""
import difflib, json, os, re, tempfile, time

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Freight Invoice Reconciler", page_icon="🚛", layout="wide")

# ------------------------------------------------------------------ PO DATA
# Supplier language lives on the PO record: the agent "knows" each supplier.
PO_DB = {
    "PO-1001": {"supplier": "Sri Lakshmi Transport", "language": "Tamil", "code": "ta-IN",
                "gst_rate": 5, "gst_type": "IGST",
                "lines": [{"desc": "Chennai-Hyderabad FTL", "qty": 2, "price": 42000},
                          {"desc": "Loading charges", "qty": 2, "price": 1500}]},
    "PO-1002": {"supplier": "Rajdhani Cargo Movers", "language": "Hindi", "code": "hi-IN",
                "gst_rate": 5, "gst_type": "IGST",
                "lines": [{"desc": "Delhi-Mumbai FTL", "qty": 1, "price": 68000}]},
    "PO-1003": {"supplier": "Godavari Logistics", "language": "Telugu", "code": "te-IN",
                "gst_rate": 18, "gst_type": "IGST",
                "lines": [{"desc": "Hyderabad-Vizag Part Load", "qty": 4, "price": 9500}]},
    "PO-1004": {"supplier": "Pune Freight Lines", "language": "Marathi", "code": "mr-IN",
                "gst_rate": 5, "gst_type": "IGST",
                "lines": [{"desc": "Pune-Bengaluru FTL", "qty": 1, "price": 52000}]},
}

for _p in PO_DB.values():
    _p.setdefault("email", "")

LANG_CODES = {"Tamil": "ta-IN", "Hindi": "hi-IN", "Telugu": "te-IN", "Marathi": "mr-IN", "Kannada": "kn-IN",
              "Malayalam": "ml-IN", "Bengali": "bn-IN", "Gujarati": "gu-IN", "Punjabi": "pa-IN", "English": "en-IN"}


def load_po_csv(file):
    """CSV columns: po_number,supplier,language,gst_rate,item,qty,price[,email,gst_type]. gst_type = IGST (inter-state, default) or CGST+SGST (intra-state). One row per PO line."""
    df = pd.read_csv(file)
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"po_number", "supplier", "language", "gst_rate", "item", "qty", "price"}
    if need - set(df.columns):
        raise ValueError("Missing columns: " + ", ".join(sorted(need - set(df.columns))))
    out = {}
    for _, r in df.iterrows():
        k = str(r["po_number"]).strip().upper().replace(" ", "")
        lang = str(r["language"]).strip().title()
        po = out.setdefault(k, {"supplier": str(r["supplier"]).strip(), "language": lang,
                                "code": LANG_CODES.get(lang, "en-IN"), "gst_rate": float(r["gst_rate"]),
                                "gst_type": ("CGST+SGST" if "cgst" in str(r.get("gst_type", "")).lower() else "IGST"),
                                "email": str(r.get("email", "") if pd.notna(r.get("email", "")) else "").strip(),
                                "lines": []})
        po["lines"].append({"desc": str(r["item"]).strip(), "qty": float(r["qty"]), "price": float(r["price"])})
    return out


# Used if extraction fails, so the demo never breaks.
FALLBACK_INVOICE = {
    "invoice_number": "INV-5521", "po_number": "PO-1001",
    "lines": [
        {"description": "Chennai-Hyderabad FTL", "quantity": 3, "unit_price": 45000},
        {"description": "Loading charges", "quantity": 2, "unit_price": 1500},
    ],
    "gst_rate": 12, "gst_amount": 16560, "igst_amount": 16560, "total": 154560,
}

TONES = {
    "Polite": "polite, warm and cooperative",
    "Firm": "firm, professional and direct",
    "Final notice": "very formal and stern, a final notice mentioning payment will be withheld until corrected",
}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string", "description": "Invoice number printed on the invoice"},
        "po_number": {"type": "string", "description": "Purchase order / PO reference number, like PO-1001"},
        "lines": {
            "type": "array", "description": "Every billed line item in the invoice table",
            "items": {"type": "object", "properties": {
                "description": {"type": "string", "description": "Line item description, e.g. route or charge name"},
                "quantity": {"type": "number", "description": "Quantity of the line item"},
                "unit_price": {"type": "number", "description": "Rate per unit in INR"},
            }},
        },
        "gst_rate": {"type": "number", "description": "GST percentage charged, e.g. 5, 12 or 18"},
        "gst_amount": {"type": "number", "description": "Total GST amount in INR"},
        "igst_amount": {"type": "number", "description": "IGST amount in INR; 0 if the invoice shows no IGST"},
        "cgst_amount": {"type": "number", "description": "CGST amount in INR; 0 if the invoice shows no CGST"},
        "sgst_amount": {"type": "number", "description": "SGST (or UTGST) amount in INR; 0 if none"},
        "total": {"type": "number", "description": "Grand total payable in INR including GST"},
    },
}


# ------------------------------------------------------------------ 1. EXTRACT
def extract_invoice(file_bytes, filename, api_key):
    """Sarvam Document AI 'extract' job -> structured invoice JSON."""
    from sarvamai import SarvamAI
    client = SarvamAI(api_subscription_key=api_key)
    mime = "application/pdf" if filename.lower().endswith(".pdf") else \
           ("image/png" if filename.lower().endswith(".png") else "image/jpeg")
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1], delete=False) as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    with open(path, "rb") as f:
        job = client.doc_ai.extract(
            file=[(filename, f, mime)],
            schema=json.dumps(INVOICE_SCHEMA),
            language="en-IN",
            output_format="json",
        )
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    for _ in range(40):  # ~2 min max
        s = client.doc_ai.get_status(job_id=job.job_id)
        if s.status.lower() in terminal:
            break
        time.sleep(3)
    if s.status.lower() not in ("completed", "partially_completed"):
        raise RuntimeError(f"Sarvam job ended as {s.status}")
    res = client.doc_ai.get_results(job_id=job.job_id).result
    if isinstance(res, str):
        res = json.loads(res)
    # some responses wrap the fields in a list / "fields" key; normalise
    if isinstance(res, list) and res:
        res = res[0]
    if isinstance(res, dict) and "lines" not in res:
        for v in res.values():
            if isinstance(v, dict) and "lines" in v:
                res = v
                break
    return res


# ------------------------------------------------------------------ 2. RECONCILE
def _num(x, default=0.0):
    try:
        return float(str(x).replace(",", "").replace("₹", ""))
    except Exception:
        return default


def reconcile(inv):
    po_no = str(inv.get("po_number", "")).strip().upper().replace(" ", "")
    po = PO_DB.get(po_no)
    if not po:
        return None, [], 0, 0
    issues, risk = [], 0.0
    po_descs = [l["desc"] for l in po["lines"]]
    matched, inv_subtotal, po_subtotal = set(), 0.0, 0.0

    for l in (inv.get("lines") or []):
        d = str(l.get("description", ""))
        q, p = _num(l.get("quantity")), _num(l.get("unit_price"))
        inv_subtotal += q * p
        close = difflib.get_close_matches(d, po_descs, n=1, cutoff=0.6)
        if not close:
            issues.append(("🔴", "Not on PO", d, f"Billed {q:g} × ₹{p:,.0f} but item is not in the PO", q * p))
            risk += q * p
            continue
        pl = next(x for x in po["lines"] if x["desc"] == close[0])
        matched.add(close[0])
        if p > pl["price"]:
            extra = (p - pl["price"]) * q
            risk += extra
            issues.append(("🔴", "Price", d, f"Rate ₹{p:,.0f} vs PO ₹{pl['price']:,}", extra))
        if q != pl["qty"]:
            extra = max(q - pl["qty"], 0) * pl["price"]
            risk += extra
            issues.append(("🟡", "Quantity", d, f"Qty {q:g} billed vs {pl['qty']} ordered", extra))

    for pl in po["lines"]:
        po_subtotal += pl["qty"] * pl["price"]

    gst = _num(inv.get("gst_rate"))
    if gst and gst != po["gst_rate"]:
        # GST overcharge on the *correct* base
        extra = po_subtotal * (gst - po["gst_rate"]) / 100
        risk += extra
        issues.append(("🔴", "GST", "Tax rate", f"Charged {gst:g}% vs agreed {po['gst_rate']}%", extra))

    # Tax structure: inter-state freight must carry IGST; intra-state must split into CGST+SGST.
    split = _num(inv.get("cgst_amount")) + _num(inv.get("sgst_amount"))
    igst = _num(inv.get("igst_amount"))
    if po.get("gst_type", "IGST") == "IGST" and split > 0:
        issues.append(("🔴", "GST type", "Tax structure",
                       f"CGST+SGST (₹{split:,.0f}) charged but IGST applies to this inter-state route; buyer cannot claim input credit", 0))
    elif po.get("gst_type") == "CGST+SGST" and igst > 0:
        issues.append(("🔴", "GST type", "Tax structure",
                       f"IGST (₹{igst:,.0f}) charged but CGST+SGST applies to this intra-state route; buyer cannot claim input credit", 0))

    expected_total = po_subtotal * (1 + po["gst_rate"] / 100)
    billed = _num(inv.get("total"))
    if billed:  # headline = what we'd overpay, tax on the overcharge included
        risk = max(billed - expected_total, 0)
    if risk <= 1:  # rounding tolerance: ignore sub-rupee differences
        risk = 0
    return po, issues, round(risk), round(expected_total)


# ------------------------------------------------------------------ 3. EMAIL
def sarvam_chat(prompt, api_key, temperature=0.4):
    r = requests.post(
        "https://api.sarvam.ai/v1/chat/completions",
        headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
        json={"model": "sarvam-30b", "temperature": temperature,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def draft_email(inv, issues, po, tone, risk, api_key):
    items = "\n".join(f"- [{k}] {d}: {msg}" + (f" (impact ₹{amt:,.0f})" if amt else "") for _, k, d, msg, amt in issues)
    prompt = f"""You are an accounts-payable executive. Write an email ENTIRELY in {po['language']} (native script)
to {po['supplier']} about invoice {inv.get('invoice_number','')} against {inv.get('po_number','')}.
Tone: {TONES[tone]}.
Discrepancies found:
{items}
Total overbilled amount: ₹{risk:,.0f}.
Keep ₹ amounts, invoice and PO numbers as numerals. Include a subject line, list each issue briefly,
and request a corrected invoice within 3 working days. Output only the email."""
    return sarvam_chat(prompt, api_key)


def translate_back(text, api_key):
    return sarvam_chat(f"Translate this email into plain English. Output only the translation:\n\n{text}", api_key, 0.1)



# ------------------------------------------------------------------ 4. STANDOUT FEATURES
import base64, urllib.parse
from concurrent.futures import ThreadPoolExecutor

HIST_FILE = "supplier_history.json"
# Seeded so the scorecard has context in a demo; real runs add to it.
SEED_HISTORY = {
    "Sri Lakshmi Transport": {"invoices": 8, "flagged": 5, "overbilled": 124000},
    "Rajdhani Cargo Movers": {"invoices": 12, "flagged": 1, "overbilled": 6800},
    "Godavari Logistics":    {"invoices": 6,  "flagged": 2, "overbilled": 21500},
}

def load_hist():
    try:
        return json.load(open(HIST_FILE))
    except Exception:
        return json.loads(json.dumps(SEED_HISTORY))

def log_invoice(po, inv, risk):
    """Record each invoice once. Returns (history, first_seen_date_or_None) - a date means DUPLICATE."""
    h = load_hist()
    seen = h.get("_seen", {})
    if isinstance(seen, list):  # legacy format
        seen = {k: "earlier" for k in seen}
    h["_seen"] = seen
    key = f"{po['supplier']}|{inv.get('invoice_number')}"
    if key in seen:
        return h, seen[key]
    seen[key] = time.strftime("%d %b %H:%M")
    r = h.setdefault(po["supplier"], {"invoices": 0, "flagged": 0, "overbilled": 0})
    r["invoices"] += 1
    r["flagged"] += 1 if risk else 0
    r["overbilled"] += risk
    json.dump(h, open(HIST_FILE, "w"))
    return h, None

def trust(rec):
    """0-100 score: clean-invoice rate, penalised by repeat offences."""
    if not rec or not rec["invoices"]:
        return 100, "🟢 New supplier"
    score = round(100 * (1 - rec["flagged"] / rec["invoices"]))
    return score, ("🟢 Reliable" if score >= 80 else "🟡 Watch closely" if score >= 50 else "🔴 High risk")

def tts(text, code, api_key):
    """Sarvam Bulbul v3 -> wav bytes. Text capped at the API's 1500-char limit."""
    body = {"text": text[:1400], "model": "bulbul:v3", "speaker": "shubh", "language_code": code}
    hdr = {"api-subscription-key": api_key, "Content-Type": "application/json"}
    r = requests.post("https://api.sarvam.ai/text-to-speech", headers=hdr, json=body, timeout=90)
    if r.status_code == 400:  # older param name
        body["target_language_code"] = body.pop("language_code")
        r = requests.post("https://api.sarvam.ai/text-to-speech", headers=hdr, json=body, timeout=90)
    r.raise_for_status()
    return base64.b64decode("".join(r.json()["audios"]))

def whatsapp_text(inv, issues, po, risk, api_key):
    items = "; ".join(f"{d}: {m}" for _, _, d, m, _ in issues)
    p = (f"Write a SHORT WhatsApp message (max 5 lines) in {po['language']} (native script) to {po['supplier']} "
         f"about invoice {inv.get('invoice_number','')} ({inv.get('po_number','')}). Issues: {items}. "
         f"Overbilled ₹{risk:,.0f}. Ask for a corrected invoice. Friendly, with one emoji. Output only the message.")
    return sarvam_chat(p, api_key)

def handle_reply(reply, po, issues, risk, api_key):
    """Read the supplier's reply in their language: translate, classify, draft the next move."""
    p = f"""A supplier ({po['supplier']}, writes in {po['language']}) replied to our invoice dispute.
Our dispute: {'; '.join(f"{d}: {m}" for _, _, d, m, _ in issues)}. Overbilled: Rs {risk}.
Supplier reply:
\"\"\"{reply}\"\"\"
Return ONLY JSON with keys:
"english": English translation of the reply,
"stance": one of "ACCEPTED", "REJECTED", "COUNTER_OFFER", "UNCLEAR",
"summary": one line on what they are saying,
"recommended_action": one line on what the AP team should do next,
"next_message": our next reply written in {po['language']} (native script), consistent with the recommended action."""
    out = sarvam_chat(p, api_key, 0.2)
    m = re.search(r"\{.*\}", out, flags=re.S)
    return json.loads(m.group(0))

DEMO_BATCH = [
    FALLBACK_INVOICE,
    {"invoice_number": "INV-0912", "po_number": "PO-1002", "gst_rate": 5, "total": 71400,
     "lines": [{"description": "Delhi-Mumbai FTL", "quantity": 1, "unit_price": 68000}]},
    {"invoice_number": "GL-778", "po_number": "PO-1003", "gst_rate": 18, "total": 48144,
     "lines": [{"description": "Hyderabad-Vizag Part Load", "quantity": 4, "unit_price": 10200}]},
    {"invoice_number": "PFL-310", "po_number": "PO-1004", "gst_rate": 5, "gst_amount": 2600,
     "cgst_amount": 1300, "sgst_amount": 1300, "total": 54600,
     "lines": [{"description": "Pune-Bengaluru FTL", "quantity": 1, "unit_price": 52000}]},
]

def decide(inv, risk, expected, dup, threshold, issues=None):
    """Turn the analysis into a payment decision: how much to release, how much to hold."""
    billed = _num(inv.get("total")) or (expected + risk)
    if dup:
        return {"code": "HOLD", "title": "🛑 HOLD EVERYTHING: possible duplicate invoice", "pay": 0, "hold": billed,
                "action": f"Invoice already processed ({dup}). Do not pay; confirm with the supplier."}
    if any(i[1] == "GST type" for i in (issues or [])):
        return {"code": "HOLD", "title": "🛑 HOLD: wrong GST type, input tax credit at risk", "pay": 0, "hold": billed,
                "action": "Do not pay. Request a corrected invoice with the right tax structure, or ITC will be blocked."}
    if risk <= 0:
        return {"code": "APPROVE", "title": "✅ APPROVE: invoice matches the PO", "pay": billed, "hold": 0,
                "action": "Release payment in the normal cycle."}
    if billed and risk / billed * 100 >= threshold:
        return {"code": "HOLD", "title": f"🛑 HOLD: overbilling is {risk / billed:.0%} of the invoice", "pay": 0, "hold": billed,
                "action": "Reject this invoice and request a corrected one."}
    return {"code": "PARTPAY", "title": "⚠️ PART-PAY: release the undisputed amount", "pay": billed - risk, "hold": risk,
            "action": "Pay the undisputed amount now, hold the disputed balance until a corrected invoice arrives."}

def report_csv(inv, po, issues, dec):
    rows = [{"invoice": inv.get("invoice_number"), "po": inv.get("po_number"), "supplier": po["supplier"],
             "type": t, "item": d, "detail": m, "impact_inr": round(a),
             "verdict": dec["code"], "pay_now_inr": round(dec["pay"]), "hold_inr": round(dec["hold"])}
            for _, t, d, m, a in issues] or [{"invoice": inv.get("invoice_number"), "po": inv.get("po_number"),
             "supplier": po["supplier"], "type": "-", "item": "-", "detail": "No mismatch", "impact_inr": 0,
             "verdict": dec["code"], "pay_now_inr": round(dec["pay"]), "hold_inr": round(dec["hold"])}]
    return pd.DataFrame(rows).to_csv(index=False)

def analyse(inv, threshold=50):
    po, issues, risk, expected = reconcile(inv)
    dec = decide(inv, risk, expected, None, threshold, issues) if po else None
    return {"inv": inv, "po": po, "issues": issues, "risk": risk, "expected": expected, "dec": dec}


# ------------------------------------------------------------------ UI
st.markdown("# 🚛 Freight Invoice Reconciler")
st.caption("Read → reconcile → dispute → negotiate, in the supplier's own language. Sarvam Vision · Sarvam-30B · Bulbul TTS")

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("Sarvam API key", type="password", value=os.environ.get("SARVAM_API_KEY", ""))
    demo = st.checkbox("Demo mode (skip Sarvam extraction)", value=not api_key)
    threshold = st.slider("Auto-hold if overbilling exceeds (% of invoice)", 10, 100, 50, 5)
    st.divider()
    st.markdown("**Purchase orders**")
    po_file = st.file_uploader("Upload your PO list (CSV)", type=["csv"], key="po_up")
    if po_file:
        try:
            loaded = load_po_csv(po_file)
            PO_DB.clear()
            PO_DB.update(loaded)
            st.success(f"Loaded {len(loaded)} POs from CSV")
        except Exception as e:
            st.error(f"PO CSV problem: {e}")
    if os.path.exists("sample_pos.csv"):
        st.download_button("⬇ Sample PO CSV format", open("sample_pos.csv").read(), file_name="sample_pos.csv")
    for k, v in PO_DB.items():
        st.markdown(f"`{k}` {v['supplier']} · {v['language']} · GST {v['gst_rate']:g}% {v.get('gst_type', 'IGST')}")
    if st.button("Reset supplier history"):
        if os.path.exists(HIST_FILE):
            os.remove(HIST_FILE)
        st.session_state.pop("inv", None)
        st.rerun()

def set_invoice(inv):
    st.session_state["inv"] = inv
    st.session_state["fresh"] = True  # next render = a new submission (drives duplicate check)
    st.session_state.pop("email", None)
    st.session_state.pop("audio", None)

tab1, tab2, tab3 = st.tabs(["🔍 Single invoice", "📊 Batch dashboard", "💬 Supplier reply agent"])

# ============================ TAB 1
with tab1:
    col_up, col_demo = st.columns([3, 1])
    up = col_up.file_uploader("Upload supplier invoice (PNG / JPG / PDF)", type=["png", "jpg", "jpeg", "pdf"], key="single")
    if col_demo.button("▶ Run with sample invoice", use_container_width=True):
        set_invoice(FALLBACK_INVOICE)

    if up and st.session_state.get("_last_file") != up.name:
        st.session_state["_last_file"] = up.name
        if demo or not api_key:
            set_invoice(FALLBACK_INVOICE)
            st.info("Demo mode: using sample extraction.")
        else:
            with st.spinner("Sarvam Vision is reading the invoice…"):
                try:
                    set_invoice(extract_invoice(up.getvalue(), up.name, api_key))
                except Exception as e:
                    st.warning(f"Extraction failed ({e}). Falling back to sample data so the demo continues.")
                    set_invoice(FALLBACK_INVOICE)

    inv = st.session_state.get("inv")
    if inv:
        po, issues, risk, expected = reconcile(inv)
        if not po:
            missing = [k for k in ("po_number", "lines", "total") if not inv.get(k)]
            st.error(f"PO '{inv.get('po_number') or '(not found on invoice)'}' is not in the PO list."
                     + (f" Extraction also missed: {', '.join(missing)}. Please verify the invoice." if missing else ""))
            st.json(inv)
            st.stop()
        if st.session_state.pop("fresh", False):
            hist, dup = log_invoice(po, inv, risk)
            st.session_state["dup"] = dup
        else:
            hist, dup = load_hist(), st.session_state.get("dup")
        dec = decide(inv, risk, expected, dup, threshold, issues)
        st.session_state["ctx"] = (inv, po, issues, risk)

        billed = _num(inv.get("total"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Supplier", po["supplier"])
        m2.metric("Billed", f"₹{billed:,.0f}")
        m3.metric("Should be", f"₹{expected:,.0f}")
        m4.metric("💰 Money at risk", f"₹{risk:,.0f}", delta="overbilled" if risk else "clean", delta_color="inverse")

        score, label = trust(hist.get(po["supplier"]))
        rec = hist.get(po["supplier"], {"invoices": 0, "flagged": 0, "overbilled": 0})
        st.markdown(f"**Supplier trust score: {score}/100 · {label}** — {rec['flagged']} of {rec['invoices']} past invoices flagged, "
                    f"₹{rec['overbilled']:,.0f} overbilled lifetime")
        st.progress(score / 100)

        banner = {"APPROVE": st.success, "PARTPAY": st.warning, "HOLD": st.error}[dec["code"]]
        banner(f"### {dec['title']}")
        v1, v2, v3 = st.columns(3)
        v1.metric("💸 Pay now", f"₹{dec['pay']:,.0f}")
        v2.metric("⏸ Hold", f"₹{dec['hold']:,.0f}")
        v3.markdown(f"**Next step:** {dec['action']}")

        st.download_button("📄 Download reconciliation report (CSV)", report_csv(inv, po, issues, dec),
                           file_name=f"reconciliation_{inv.get('invoice_number','invoice')}.csv")
        left, right = st.columns(2)
        with left:
            st.subheader("Extracted by Sarvam Vision")
            st.dataframe(pd.DataFrame(inv.get("lines", [])), hide_index=True, use_container_width=True)
            with st.expander("Raw JSON"):
                st.json(inv)
        with right:
            st.subheader("Mismatches")
            if issues:
                st.dataframe(pd.DataFrame(issues, columns=["", "Type", "Item", "Detail", "Impact ₹"]),
                             hide_index=True, use_container_width=True)

        if issues:
            st.divider()
            st.subheader(f"✉️ Dispute in {po['language']}")
            c1, c2 = st.columns([2, 1])
            tone = c1.select_slider("Tone", list(TONES), value="Firm")
            show_en = c2.checkbox("English for my review", value=True)
            if st.button(f"Draft in {po['language']}", type="primary"):
                if not api_key:
                    st.warning("Add your Sarvam API key in the sidebar to draft emails.")
                else:
                    with st.spinner(f"Writing in {po['language']}…"):
                        try:
                            mail = draft_email(inv, issues, po, tone, risk, api_key)
                            st.session_state["email"] = {
                                "mail": mail,
                                "en": translate_back(mail, api_key) if show_en else None,
                                "wa": whatsapp_text(inv, issues, po, risk, api_key)}
                            st.session_state.pop("audio", None)
                        except Exception as e:
                            st.error(f"Drafting failed: {e}")
            em = st.session_state.get("email")
            if em:
                a, b = st.columns(2)
                a.text_area(f"{po['language']} (send this)", em["mail"], height=300)
                if em["en"]:
                    b.text_area("English (for your review)", em["en"], height=300)

                st.markdown("**Send it where suppliers actually read it**")
                w1, w2, w3, w4 = st.columns(4)
                w1.link_button("📲 Send on WhatsApp", "https://wa.me/?text=" + urllib.parse.quote(em["wa"]),
                               use_container_width=True)
                if w2.button(f"🔊 Hear it in {po['language']}", use_container_width=True):
                    with st.spinner("Bulbul is speaking…"):
                        try:
                            st.session_state["audio"] = tts(em["wa"], po["code"], api_key)
                        except Exception as e:
                            st.error(f"TTS failed: {e}")
                subj = f"Invoice {inv.get('invoice_number','')} / {inv.get('po_number','')}: discrepancy, corrected invoice requested"
                w3.link_button("✉️ Open in email", f"mailto:{po.get('email','')}?subject={urllib.parse.quote(subj)}&body="
                               + urllib.parse.quote(em["mail"][:1800]), use_container_width=True)
                w4.download_button("⬇ Email .txt", em["mail"], file_name="dispute_email.txt", use_container_width=True)
                with st.expander("WhatsApp version"):
                    st.write(em["wa"])
                if st.session_state.get("audio"):
                    st.audio(st.session_state["audio"], format="audio/wav")
    else:
        st.info("Upload an invoice or click **Run with sample invoice** to see the agent work.")

# ============================ TAB 2
with tab2:
    st.subheader("AP inbox → one dashboard")
    files = st.file_uploader("Drop several invoices", type=["png", "jpg", "jpeg", "pdf"],
                             accept_multiple_files=True, key="batch_up")
    cA, cB = st.columns(2)
    run_demo = cA.button("▶ Load 3 sample invoices", use_container_width=True)
    run_real = cB.button("Process uploaded invoices", use_container_width=True, disabled=not files)

    if run_demo:
        st.session_state["batch_res"] = [analyse(i, threshold) for i in DEMO_BATCH]
    if run_real:
        def work(f):
            try:
                if demo or not api_key:
                    return analyse(FALLBACK_INVOICE, threshold)
                return analyse(extract_invoice(f.getvalue(), f.name, api_key), threshold)
            except Exception as e:
                return {"error": f"{f.name}: {e}"}
        with st.spinner(f"Reading {len(files)} invoices in parallel…"):
            with ThreadPoolExecutor(3) as ex:
                st.session_state["batch_res"] = list(ex.map(work, files))

    res = st.session_state.get("batch_res")
    if res:
        for r in res:
            if "error" in r:
                st.warning(r["error"])
        ok = [r for r in res if "error" not in r and r["po"]]
        if ok:
            total_risk = sum(r["risk"] for r in ok)
            flagged = sum(1 for r in ok if r["dec"]["code"] != "APPROVE")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Invoices processed", len(ok))
            k2.metric("Flagged", f"{flagged} / {len(ok)}")
            k3.metric("💸 Release now", f"₹{sum(r['dec']['pay'] for r in ok):,.0f}")
            k4.metric("⏸ Hold / recover", f"₹{sum(r['dec']['hold'] for r in ok):,.0f}")
            rows = [{"Status": {"APPROVE": "🟢", "PARTPAY": "🟡", "HOLD": "🔴"}[r["dec"]["code"]], "Invoice": r["inv"].get("invoice_number"),
                     "Supplier": r["po"]["supplier"], "Language": r["po"]["language"],
                     "Verdict": r["dec"]["code"], "Billed ₹": _num(r["inv"].get("total")), "At risk ₹": r["risk"],
                     "Issues": ", ".join(sorted({i[1] for i in r["issues"]})) or "—"} for r in ok]
            df = pd.DataFrame(rows).sort_values("At risk ₹", ascending=False)
            st.dataframe(df, hide_index=True, use_container_width=True)
            st.bar_chart(df.groupby("Supplier")["At risk ₹"].sum())
            st.caption("Sorted by money at risk, so the AP team works the biggest leaks first.")
    else:
        st.info("Load the sample batch to see the portfolio view.")

# ============================ TAB 3
with tab3:
    st.subheader("Supplier replied? The agent reads it in their language")
    ctx = st.session_state.get("ctx")
    if not ctx:
        st.info("Analyse an invoice in the first tab so the agent knows what is in dispute.")
    else:
        inv, po, issues, risk = ctx
        st.write(f"Dispute with **{po['supplier']}** · ₹{risk:,.0f} · reply language: **{po['language']}**")
        SAMPLE_REPLY = {
            "Tamil": "வணக்கம், எங்கள் தவறுக்கு வருந்துகிறோம். GST 5% ஆக திருத்தி புதிய பில் அனுப்புகிறோம். ஆனால் மூன்றாவது லாரிக்கான கட்டணம் ஏற்கனவே அனுமதிக்கப்பட்டது.",
            "Hindi": "नमस्ते, हम GST सही कर देंगे लेकिन रेट में बदलाव संभव नहीं है।",
            "Telugu": "నమస్కారం, రేటు పెరుగుదల ఇంధన ధరల వల్ల. దయచేసి చెల్లించండి.",
            "Marathi": "नमस्कार, चूक झाली. आम्ही IGST सह दुरुस्त केलेले बिल आजच पाठवतो."}
        if st.button("Paste a sample supplier reply"):
            st.session_state["reply_txt"] = SAMPLE_REPLY[po["language"]]
        reply = st.text_area(f"Supplier's reply ({po['language']})", key="reply_txt", height=120)
        if st.button("Analyse reply", type="primary", disabled=not reply):
            if not api_key:
                st.warning("Add your Sarvam API key in the sidebar.")
            else:
                with st.spinner("Reading the reply…"):
                    try:
                        st.session_state["reply_out"] = handle_reply(reply, po, issues, risk, api_key)
                    except Exception as e:
                        st.error(f"Could not analyse reply: {e}")
        ro = st.session_state.get("reply_out")
        if ro:
            icon = {"ACCEPTED": "✅", "REJECTED": "❌", "COUNTER_OFFER": "🤝", "UNCLEAR": "❓"}.get(ro.get("stance"), "❓")
            st.markdown(f"### {icon} {ro.get('stance')}")
            st.write("**In English:**", ro.get("english"))
            st.write("**Summary:**", ro.get("summary"))
            st.info("**Recommended action:** " + str(ro.get("recommended_action")))
            st.text_area(f"Our next reply ({po['language']})", ro.get("next_message", ""), height=200)