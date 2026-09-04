"""
Invoice email parser — connects to an IMAP mailbox (e.g. billing@yourco.com),
fetches vendor invoices, extracts amounts and dates, and stores them as cost
entries. Solves the SaaS billing data gap for vendors with no public billing API
(PagerDuty, GitHub Enterprise, New Relic, etc.).

Setup:
  finops setup invoice    →  stores IMAP credentials in vault

How it works:
  1. Connect to IMAP over TLS
  2. Search for unread emails in FINOPS_INVOICE_FOLDER (default: INBOX)
  3. For each email: try HTML body first, then PDF attachments
  4. Extract: vendor name, invoice total, currency, invoice date
  5. Store as CostEntry(provider="invoice/<vendor>", ...)
  6. Mark email as read / move to processed folder

Extraction strategy:
  - Pattern match on common invoice formats (Stripe, AWS, GCP, Datadog, etc.)
  - Generic fallback: find "$X,XXX.XX" or "Total Due: X" patterns
  - PDF: extract text with pdfplumber if available, fall back to pypdf
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import io
from dataclasses import dataclass
from datetime import date, datetime
from email.header import decode_header
from typing import Any

log = logging.getLogger(__name__)

# ── Amount extraction patterns ────────────────────────────────────────────────

_AMOUNT_PATTERNS = [
    # "Total Due: $1,234.56" / "Amount Due $1,234.56"
    r"(?:total\s+(?:due|amount|billed)|amount\s+due|invoice\s+total)[:\s]+\$?([\d,]+\.?\d*)",
    # "Total: $1,234.56"
    r"\btotal[:\s]+\$?([\d,]+\.?\d{2})\b",
    # "$1,234.56" standalone (last resort — take largest)
    r"\$\s*([\d,]+\.?\d{2})",
]

# ── Vendor detection ──────────────────────────────────────────────────────────

_VENDOR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pagerduty", re.I), "pagerduty"),
    (re.compile(r"new\s*relic", re.I), "new_relic"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"datadog", re.I), "datadog"),
    (re.compile(r"snowflake", re.I), "snowflake"),
    (re.compile(r"vercel", re.I), "vercel"),
    (re.compile(r"twilio", re.I), "twilio"),
    (re.compile(r"mongodb\s*atlas|mlab", re.I), "mongodb_atlas"),
    (re.compile(r"cloudflare", re.I), "cloudflare"),
    (re.compile(r"stripe", re.I), "stripe"),
    (re.compile(r"amazon\s+web\s+services|aws", re.I), "aws"),
    (re.compile(r"microsoft\s+azure|azure", re.I), "azure"),
    (re.compile(r"google\s+cloud|gcp", re.I), "gcp"),
    (re.compile(r"heroku", re.I), "heroku"),
    (re.compile(r"sendgrid", re.I), "sendgrid"),
    (re.compile(r"splunk", re.I), "splunk"),
    (re.compile(r"hashicorp", re.I), "hashicorp"),
    (re.compile(r"elastic", re.I), "elastic"),
    (re.compile(r"confluent", re.I), "confluent"),
    (re.compile(r"planetscale", re.I), "planetscale"),
    (re.compile(r"supabase", re.I), "supabase"),
    (re.compile(r"neon", re.I), "neon"),
]


@dataclass
class ParsedInvoice:
    vendor: str
    amount_usd: float
    invoice_date: date
    currency: str = "USD"
    invoice_number: str = ""
    raw_text: str = ""
    source_email_id: str = ""


def _detect_vendor(text: str, sender: str) -> str:
    combined = f"{sender} {text[:2000]}"
    for pattern, name in _VENDOR_PATTERNS:
        if pattern.search(combined):
            return name
    # Fall back to sender domain
    m = re.search(r"@([\w.-]+\.\w+)", sender)
    if m:
        domain = m.group(1).lower()
        domain = re.sub(r"\.(com|io|net|org|co)$", "", domain)
        return domain.replace(".", "_")
    return "unknown"


def _extract_amount(text: str) -> float | None:
    for pattern in _AMOUNT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            amounts = [float(m.replace(",", "")) for m in matches if m]
            if amounts:
                return max(amounts)
    return None


def _extract_date(text: str) -> date:
    # Common invoice date patterns
    patterns = [
        r"(?:invoice|billing)\s+date[:\s]+(\w+ \d{1,2},?\s+\d{4})",
        r"(?:invoice|billing)\s+date[:\s]+(\d{4}-\d{2}-\d{2})",
        r"(?:issue[d]?\s+date|date\s+issued)[:\s]+(\w+ \d{1,2},?\s+\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt).date()
                except ValueError:
                    continue
    return date.today()


def _extract_invoice_number(text: str) -> str:
    m = re.search(r"invoice\s*(?:#|number|no\.?)[:\s]*([A-Z0-9\-]+)", text, re.IGNORECASE)
    return m.group(1) if m else ""


def _text_from_pdf_bytes(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except ImportError:
        pass
    return ""


def _decode_header_value(val: str) -> str:
    parts = decode_header(val)
    return "".join(
        p.decode(enc or "utf-8") if isinstance(p, bytes) else p
        for p, enc in parts
    )


def _parse_email_message(msg: email.message.Message, uid: str) -> ParsedInvoice | None:
    sender = _decode_header_value(msg.get("From", ""))
    subject = _decode_header_value(msg.get("Subject", ""))
    date_header = msg.get("Date", "")

    body_text = ""
    pdf_texts: list[str] = []

    for part in msg.walk():
        ct = part.get_content_type()
        disp = str(part.get("Content-Disposition", ""))

        if ct in ("text/plain", "text/html") and "attachment" not in disp:
            payload = part.get_payload(decode=True)
            if payload:
                body_text += payload.decode(part.get_content_charset() or "utf-8", errors="replace")

        elif ct == "application/pdf" or (
            "attachment" in disp and part.get_filename("").lower().endswith(".pdf")
        ):
            payload = part.get_payload(decode=True)
            if payload:
                pdf_texts.append(_text_from_pdf_bytes(payload))

    # Clean HTML tags from body
    body_text = re.sub(r"<[^>]+>", " ", body_text)
    body_text = re.sub(r"\s+", " ", body_text)

    full_text = f"{subject} {sender} {body_text} " + " ".join(pdf_texts)

    amount = _extract_amount(full_text)
    if amount is None or amount <= 0:
        return None

    return ParsedInvoice(
        vendor=_detect_vendor(full_text, sender),
        amount_usd=amount,
        invoice_date=_extract_date(full_text),
        invoice_number=_extract_invoice_number(full_text),
        raw_text=full_text[:500],
        source_email_id=uid,
    )


# ── IMAP fetcher ──────────────────────────────────────────────────────────────

class InvoiceMailbox:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        folder: str = "INBOX",
        processed_folder: str = "INBOX/FinOps-Processed",
        port: int = 993,
    ) -> None:
        self.host = host
        self.user = user
        self.password = password
        self.folder = folder
        self.processed_folder = processed_folder
        self.port = port
        self._conn: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        self._conn.login(self.user, self.password)

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def fetch_invoices(self, search: str = "UNSEEN SUBJECT invoice") -> list[ParsedInvoice]:
        if not self._conn:
            self.connect()
        assert self._conn is not None

        self._conn.select(self.folder)
        _, data = self._conn.search(None, search)
        uid_list = data[0].split() if data[0] else []

        results: list[ParsedInvoice] = []
        for uid in uid_list:
            _, msg_data = self._conn.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            parsed = _parse_email_message(msg, uid.decode())
            if parsed:
                results.append(parsed)
                self._mark_processed(uid)
        return results

    def _mark_processed(self, uid: bytes) -> None:
        assert self._conn is not None
        # Try to move to processed folder; if it doesn't exist just mark read
        try:
            self._conn.create(self.processed_folder)
        except Exception:
            pass
        try:
            self._conn.copy(uid, self.processed_folder)
            self._conn.store(uid, "+FLAGS", "\\Deleted")
            self._conn.expunge()
        except Exception:
            self._conn.store(uid, "+FLAGS", "\\Seen")


def _store_invoice(inv: ParsedInvoice, account_id: str) -> dict[str, Any]:
    """Persist one parsed invoice as a daily cost snapshot and return its summary.

    store_snapshot takes the snapshot columns by name, not a cost-entry object.
    Mapping the fields explicitly here is what turns a parsed email into a row
    the rest of nable can query.
    """
    from ...storage.snapshots import store_snapshot

    store_snapshot(
        provider=f"invoice/{inv.vendor}",
        service="invoice",
        account_id=account_id,
        region="",
        snapshot_date=inv.invoice_date,
        amount_usd=inv.amount_usd,
    )
    return {
        "vendor": inv.vendor,
        "amount_usd": inv.amount_usd,
        "invoice_date": inv.invoice_date.isoformat(),
        "invoice_number": inv.invoice_number,
    }


def fetch_and_store_invoices() -> list[dict[str, Any]]:
    """
    Called by the scheduler. Fetches invoice emails, parses them, stores as
    cost snapshots. Returns list of stored invoice dicts.
    """
    host = os.environ.get("FINOPS_INVOICE_IMAP_HOST", "")
    if not host:
        return []

    user = os.environ.get("FINOPS_INVOICE_IMAP_USER", "")
    password = os.environ.get("FINOPS_INVOICE_IMAP_PASSWORD", "")
    folder = os.environ.get("FINOPS_INVOICE_FOLDER", "INBOX")
    search = os.environ.get("FINOPS_INVOICE_SEARCH", "UNSEEN SUBJECT invoice")

    mailbox = InvoiceMailbox(host=host, user=user, password=password, folder=folder)
    stored: list[dict[str, Any]] = []
    try:
        # A mailbox that is down or misconfigured is an expected, recoverable
        # miss: log it and move on. A failure while writing a snapshot is a real
        # defect, so it is deliberately NOT caught here and surfaces to the
        # caller instead of being logged away as "fetch failed".
        try:
            mailbox.connect()
            invoices = mailbox.fetch_invoices(search)
        except (imaplib.IMAP4.error, OSError) as e:
            log.error("Invoice fetch failed: %s", e)
            return stored

        for inv in invoices:
            stored.append(_store_invoice(inv, user))
    finally:
        mailbox.disconnect()

    return stored
