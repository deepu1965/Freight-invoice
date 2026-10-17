# 🚛 Freight-Invoice Reconciliation Agent

An AI agent that reads a freight invoice, checks it against the purchase order, decides what to pay, and disputes the supplier **in their own language**. It runs on the Sarvam AI stack and a Streamlit UI.

> Built for a hackathon. Prototype scope: see [Limitations](#limitations--next-steps).

## What it does

| Step | What happens | Powered by |
|---|---|---|
| 1. Read | Extracts invoice number, PO number, line items, GST rate and amounts (IGST / CGST / SGST), total | Sarvam Vision (Document AI **Extract**) |
| 2. Reconcile | Matches lines to the PO (fuzzy) and flags **price**, **quantity**, **GST rate**, **GST type** (IGST vs CGST+SGST), items not on the PO | Python |
| 3. Decide | **Approve / Part-pay / Hold**, with exact ₹ to release and ₹ to hold. Flags **duplicate invoices** | Python |
| 4. Dispute | Drafts the email in the supplier's language (Tamil, Hindi, Telugu, Marathi, …) with a tone selector (Polite / Firm / Final notice) and an English back-translation for review | Sarvam-30B |
| 5. Send | Opens WhatsApp or the mail client prefilled. Reads the message aloud in the supplier's language | Sarvam Bulbul v3 |
| 6. Negotiate | Paste the supplier's reply: it is translated, classified (accepted / rejected / counter-offer) and the next reply is drafted | Sarvam-30B |

Also included: **batch dashboard** (many invoices, one view, ranked by money at risk), **supplier trust score** with history, **reconciliation report** download (CSV), and **PO list upload** (CSV).

## Quick start

```bash
git clone https://github.com/deepu1965/Freight-invoice.git
cd Freight-invoice
pip install -r requirements.txt
export SARVAM_API_KEY="your_key"      # Windows PowerShell: $env:SARVAM_API_KEY="your_key"
streamlit run app.py
```

Open http://localhost:8501. Without a key the app runs in **Demo mode** using built-in sample data. You can also paste the key in the sidebar.

Get a key at https://dashboard.sarvam.ai.

## Demo script (3 minutes)

1. Sidebar → **Reset supplier history**.
2. **Batch dashboard** tab → *Load 3 sample invoices* (loads 4). Show release-now vs hold.
3. **Single invoice** tab → *Run with sample invoice*: part-pay verdict, trust score, mismatches.
4. *Draft in Tamil* → *Hear it* → *Send on WhatsApp*.
5. Click *Run with sample invoice* again → **duplicate hold**.
6. **Supplier reply agent** tab → *Paste a sample supplier reply* → *Analyse reply*.

Sample files: `sample_invoice_1.png` (price + quantity + GST rate errors), `sample_invoice_gsttype.png` (wrong GST type), `sample_invoice_telugu.png` (price error), `sample_invoice_clean.png` (no errors).

## PO list format (CSV)

One row per PO line. Download a template from the sidebar or see `sample_pos.csv`.

```
po_number,supplier,language,gst_rate,item,qty,price,email,gst_type
PO-1001,Sri Lakshmi Transport,Tamil,5,Chennai-Hyderabad FTL,2,42000,accounts@example.com,IGST
```

`gst_type` is `IGST` (inter-state, default) or `CGST+SGST` (intra-state). `email` and `gst_type` are optional.

## How "money at risk" is computed

`billed total − (PO quantity × PO price) × (1 + agreed GST%)`, so tax on the overcharge is included. Differences of ₹1 or less are ignored. Per-issue impacts are shown in the mismatch table.

## Project layout

```
app.py              # whole app: extraction, reconcile, decision engine, email/TTS/reply agent, UI
sample_pos.csv      # PO list template
make_samples.py     # regenerates the sample invoice images (needs Pillow)
requirements.txt
```

## Deploy (Streamlit Community Cloud)

1. Push this repo to GitHub.
2. Go to https://share.streamlit.io → **New app** → select the repo, branch `main`, main file `app.py`.
3. **Advanced settings → Secrets**, add:
   ```toml
   SARVAM_API_KEY = "your_key"
   ```
4. Deploy. The app reads the key from secrets automatically.

## Limitations & next steps

- Supplier history is stored in a local JSON file. On Streamlit Cloud it resets when the app restarts. Next: a real database.
- POs come from a CSV, not an ERP. Next: Tally / SAP / Zoho integration.
- Email and WhatsApp open prefilled; they are not sent automatically. Next: SMTP and the WhatsApp Business API with an approval step.
- Reconciliation is rule-based, with fuzzy line-item matching. Next: handle freight surcharges (fuel, detention) and multi-PO invoices.
- Invoice extraction is limited to PDF/PNG/JPG, up to 10 pages per file.
- Suppliers are not authenticated, and there is no role-based approval workflow yet.

## Tech

Streamlit · Sarvam Document AI (Vision 1.5) · Sarvam-30B · Sarvam Bulbul v3 · pandas
