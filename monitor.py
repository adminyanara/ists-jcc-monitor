import os
import json
import time
import smtplib
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pdfplumber
import anthropic


# ══════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════
CTUIL_URLS = [
    "https://ctuil.in/ists-joint-coordination-meeting",
    "https://ctuil.in/ists-consultation-meeting",
]
PROCESSED_FILE = "data/processed_pdfs.json"
PROMPT_FILE = "prompt.md"
DIGEST_DIR = "digests"
DATA_DIR = "data"
MAX_CHARS_PER_PDF = 8000
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 4096

# Only download PDFs from these domains
ALLOWED_DOMAINS = [
    "ctuil.in",
    "www.ctuil.in",
]

# Keywords to INCLUDE — PDF link text must contain at least one
# (case-insensitive). These target JCC meeting documents only.
INCLUDE_KEYWORDS = [
    "jcc",
    "joint coordination",
    "project review",
    "prm",
    "meeting minutes",
    "meeting notice",
    "agenda",
    "minutes of meeting",
    "mom",
    "action taken report",
    "atr",
    "commissioning",
    "coordination committee",
    "ists",
]

# Keywords to EXCLUDE — skip PDFs matching these
# (case-insensitive). Filters out noise documents.
EXCLUDE_KEYWORDS = [
    "holiday",
    "calendar",
    "calender",
    "advertisement",
    "recruitment",
    "non executive",
    "executive posts",
    "vacancy",
    "tender",
    "faq",
    "common errors",
    "sop for connectivity portal",
]

DOWNLOAD_DELAY = 2
MAX_RETRIES = 3


# ══════════════════════════════════════════════
# HELPER: CREATE A RESILIENT HTTP SESSION
# ══════════════════════════════════════════════
def create_session():
    """Create a requests session with retry logic."""
    session = requests.Session()

    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })

    return session


# ══════════════════════════════════════════════
# HELPER: CHECK IF URL IS FROM ALLOWED DOMAIN
# ══════════════════════════════════════════════
def is_allowed_domain(url):
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    return any(domain == allowed or domain.endswith(f".{allowed}")
               for allowed in ALLOWED_DOMAINS)


# ══════════════════════════════════════════════
# HELPER: CHECK IF PDF IS RELEVANT (JCC-RELATED)
# ══════════════════════════════════════════════
def is_relevant_pdf(link_text):
    """Check if a PDF link text matches JCC-related keywords."""
    text_lower = link_text.lower()

    # First check exclusions
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text_lower:
            return False

    # Then check inclusions
    for keyword in INCLUDE_KEYWORDS:
        if keyword in text_lower:
            return True

    # Default: exclude (be conservative to save API costs)
    return False


# ══════════════════════════════════════════════
# EMAIL METHOD DETECTION
# ══════════════════════════════════════════════
def get_email_method():
    if os.environ.get("AZURE_CLIENT_ID") and os.environ.get("AZURE_TENANT_ID"):
        return "graph"
    elif os.environ.get("SMTP_USERNAME") and os.environ.get("SMTP_PASSWORD"):
        return "smtp"
    else:
        return None


# ══════════════════════════════════════════════
# STEP 1: FETCH PAGE AND EXTRACT PDF LINKS
# ══════════════════════════════════════════════
def fetch_pdf_links(session, url):
    """Scrape a CTUIL page for relevant PDF links only."""
    print(f"[1/6] Fetching page: {url}")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    pdf_links = []
    seen_urls = set()          # Deduplicate
    skipped_external = 0
    skipped_irrelevant = 0
    skipped_duplicate = 0

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.lower().endswith(".pdf"):
            # Build full URL
            if href.startswith("/"):
                url = f"https://ctuil.in{href}"
            elif not href.startswith("http"):
                url = f"https://ctuil.in/{href}"
            else:
                url = href

            # Skip external domains
            if not is_allowed_domain(url):
                skipped_external += 1
                continue

            # Skip duplicates
            if url in seen_urls:
                skipped_duplicate += 1
                continue
            seen_urls.add(url)

            link_text = a_tag.get_text(strip=True) or url.split("/")[-1]

            # Skip irrelevant documents
            if not is_relevant_pdf(link_text):
                skipped_irrelevant += 1
                continue

            pdf_links.append({"url": url, "text": link_text})

    print(f"      Found {len(pdf_links)} relevant PDF link(s).")
    if skipped_external > 0:
        print(f"      Skipped {skipped_external} external domain link(s).")
    if skipped_duplicate > 0:
        print(f"      Skipped {skipped_duplicate} duplicate link(s).")
    if skipped_irrelevant > 0:
        print(f"      Skipped {skipped_irrelevant} irrelevant link(s) "
              f"(holidays, ads, FAQs, etc.).")
    return pdf_links


# ══════════════════════════════════════════════
# STEP 2: DOWNLOAD PDF AND EXTRACT TEXT
# ══════════════════════════════════════════════
def download_and_extract(session, pdf_url):
    """Download a PDF and extract text. Returns text or error string."""
    try:
        resp = session.get(pdf_url, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        return f"[DOWNLOAD FAILED — Connection rejected: {e}]"
    except requests.exceptions.Timeout:
        return f"[DOWNLOAD FAILED — Timed out after 120s]"
    except requests.exceptions.HTTPError as e:
        return f"[DOWNLOAD FAILED — HTTP {e.response.status_code}]"
    except Exception as e:
        return f"[DOWNLOAD FAILED — {type(e).__name__}: {e}]"

    tmp_path = "/tmp/ists_temp.pdf"
    with open(tmp_path, "wb") as f:
        f.write(resp.content)

    if resp.content[:5] != b"%PDF-":
        return "[DOWNLOAD FAILED — Server returned non-PDF content]"

    text = ""
    try:
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        return f"[PDF EXTRACTION FAILED — {type(e).__name__}: {e}]"

    return text.strip() if text.strip() else \
        "[PDF EXTRACTION FAILED — No text found (scanned image?)]"


# ══════════════════════════════════════════════
# STEP 3: ANALYZE WITH CLAUDE
# ══════════════════════════════════════════════
def analyze_with_claude(extracted_texts):
    """Send extracted PDF text to Claude for analysis."""
    print("[4/6] Running Claude analysis...")

    with open(PROMPT_FILE, "r", encoding="utf-8") as f:
        system_prompt = f.read()

    combined_parts = []
    for t in extracted_texts:
        content = t["content"]
        if len(content) > MAX_CHARS_PER_PDF:
            content = content[:MAX_CHARS_PER_PDF] + "\n\n[... truncated ...]"
        combined_parts.append(
            f"**{t['name']}** (URL: {t['url']}):\n{content}"
        )

    user_message = "\n\n---\n\n".join(combined_parts)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Today's date: {datetime.now().strftime('%Y-%m-%d')}.\n\n"
                    f"Below are extracted texts from CTUIL ISTS JCC meeting "
                    f"documents. Produce the digest as per your instructions."
                    f"\n\n{user_message}"
                ),
            }
        ],
    )

    digest = ""
    for block in message.content:
        if block.type == "text":
            digest += block.text

    print(f"      Claude response: {len(digest)} characters.")
    return digest


# ══════════════════════════════════════════════
# STEP 4A: SEND EMAIL VIA MICROSOFT GRAPH API
# ══════════════════════════════════════════════
def get_graph_access_token():
    import msal

    tenant_id = os.environ["AZURE_TENANT_ID"]
    client_id = os.environ["AZURE_CLIENT_ID"]
    client_secret = os.environ["AZURE_CLIENT_SECRET"]

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception(
            f"Failed to get token: {result.get('error')}: "
            f"{result.get('error_description')}"
        )


def send_email_graph(digest, new_pdf_count, failed_count, recipients):
    sender_email = os.environ["SENDER_EMAIL"]
    print(f"[5/6] Sending email via Microsoft Graph API "
          f"from {sender_email}...")

    access_token = get_graph_access_token()
    date_str = datetime.now().strftime("%Y-%m-%d")
    html_body = build_html_email(
        digest, new_pdf_count, failed_count, date_str
    )

    to_recipients = [
        {"emailAddress": {"address": email.strip()}}
        for email in recipients
    ]

    email_payload = {
        "message": {
            "subject": (
                f"ISTS JCC Digest — {date_str} "
                f"({new_pdf_count} new documents)"
            ),
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": to_recipients,
        },
        "saveToSentItems": "true",
    }

    response = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=email_payload,
    )

    if response.status_code == 202:
        print(f"      Email sent to {len(recipients)} recipient(s).")
    else:
        print(f"      ERROR: Graph API returned {response.status_code}")
        print(f"      {response.text}")


# ══════════════════════════════════════════════
# STEP 4B: SEND EMAIL VIA SMTP AUTH (OFFICE 365)
# ══════════════════════════════════════════════
def send_email_smtp(digest, new_pdf_count, failed_count, recipients):
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    print(f"[5/6] Sending email via SMTP AUTH from {smtp_username}...")

    date_str = datetime.now().strftime("%Y-%m-%d")
    html_body = build_html_email(
        digest, new_pdf_count, failed_count, date_str
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"ISTS JCC Digest — {date_str} "
        f"({new_pdf_count} new documents)"
    )
    msg["From"] = smtp_username
    msg["To"] = ", ".join(recipients)

    plain_body = (
        f"ISTS JCC Digest — {date_str}\n"
        f"New documents processed: {new_pdf_count}\n"
        f"Failed downloads: {failed_count}\n\n"
        f"{digest}\n\n---\n"
        f"Sources: {', '.join(CTUIL_URLS)}"
    )

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.office365.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_username, smtp_password)
            server.sendmail(smtp_username, recipients, msg.as_string())
        print(f"      Email sent to {len(recipients)} recipient(s).")
    except smtplib.SMTPAuthenticationError as e:
        print(f"      ERROR: SMTP authentication failed: {e}")
        print(f"      Try: App Password or switch to Graph API.")
    except Exception as e:
        print(f"      ERROR sending email: {e}")


# ══════════════════════════════════════════════
# SHARED: BUILD HTML EMAIL
# ══════════════════════════════════════════════
def build_html_email(digest, new_pdf_count, failed_count, date_str):
    import html as html_module
    escaped_digest = html_module.escape(digest)

    lines = escaped_digest.split("\n")
    formatted_lines = []
    for line in lines:
        if line.startswith("## "):
            formatted_lines.append(
                f'<h2 style="color:#2c3e50;margin-top:25px;'
                f'border-bottom:1px solid #eee;padding-bottom:5px;">'
                f'{line[3:]}</h2>'
            )
        elif line.startswith("# "):
            formatted_lines.append(
                f'<h1 style="color:#1a5276;">{line[2:]}</h1>'
            )
        elif line.startswith("| "):
            formatted_lines.append(
                f'<code style="font-size:0.9em;">{line}</code><br>'
            )
        elif line.strip() == "":
            formatted_lines.append("<br>")
        else:
            formatted_lines.append(f"<p style='margin:4px 0;'>{line}</p>")

    body_content = "\n".join(formatted_lines)

    warning_banner = ""
    if failed_count > 0:
        warning_banner = f"""
        <div style="background-color:#fff3cd;border:1px solid #ffc107;
                    border-radius:5px;padding:10px 15px;margin-bottom:15px;">
            ⚠️ <strong>{failed_count} PDF(s) failed to download</strong>
            — will retry in the next run.
        </div>
        """

    return f"""
    <html>
    <head>
        <style>
            body {{
                font-family: 'Segoe UI', Calibri, Arial, sans-serif;
                line-height: 1.6; color: #333;
                max-width: 850px; margin: 0 auto; padding: 20px;
            }}
            .header {{
                background: linear-gradient(135deg, #1a5276, #2980b9);
                color: white; padding: 20px 25px;
                border-radius: 8px 8px 0 0;
            }}
            .header h1 {{
                color: white; margin: 0; font-size: 1.4em;
            }}
            .badge {{
                display: inline-block; background-color: #27ae60;
                color: white; padding: 3px 10px;
                border-radius: 4px; font-size: 0.85em; margin-top: 8px;
            }}
            .badge-warn {{
                display: inline-block; background-color: #e67e22;
                color: white; padding: 3px 10px;
                border-radius: 4px; font-size: 0.85em; margin-top: 8px;
                margin-left: 5px;
            }}
            .content {{
                background: #ffffff; padding: 25px;
                border: 1px solid #e0e0e0; border-top: none;
            }}
            .footer {{
                background: #f8f9fa; padding: 15px 25px;
                border: 1px solid #e0e0e0; border-top: none;
                border-radius: 0 0 8px 8px;
                font-size: 0.85em; color: #777;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1 style="color:white; border:none; margin:0;">
                ISTS JCC Digest — {date_str}
            </h1>
            <span class="badge">
                {new_pdf_count} document(s) processed
            </span>
            {"<span class='badge-warn'>" + str(failed_count)
             + " failed</span>" if failed_count > 0 else ""}
        </div>
        <div class="content">
            {warning_banner}
            {body_content}
        </div>
        <div class="footer">
            <p><strong>Generated by ISTS JCC Monitor</strong><br>
            Sources: {', '.join(CTUIL_URLS)}<br>
            This is an automated digest.</p>
        </div>
    </body>
    </html>
    """


# ══════════════════════════════════════════════
# STEP 4 DISPATCHER
# ══════════════════════════════════════════════
def send_email_notification(digest, new_pdf_count, failed_count):
    notify_emails_raw = os.environ.get("NOTIFY_EMAILS", "")
    if not notify_emails_raw:
        print("[SKIP] NOTIFY_EMAILS not set. Skipping email.")
        return

    recipients = [
        e.strip() for e in notify_emails_raw.split(",") if e.strip()
    ]
    if not recipients:
        print("[SKIP] No valid email addresses in NOTIFY_EMAILS.")
        return

    print(f"      Recipients: {', '.join(recipients)}")
    method = get_email_method()

    if method == "graph":
        send_email_graph(digest, new_pdf_count, failed_count, recipients)
    elif method == "smtp":
        send_email_smtp(digest, new_pdf_count, failed_count, recipients)
    else:
        print("[SKIP] No email credentials configured "
              "(need AZURE_* or SMTP_* secrets). Skipping email.")


# ══════════════════════════════════════════════
# STEP 5 (OPTIONAL): WRITE TO NOTION
# ══════════════════════════════════════════════
def write_to_notion(digest):
    api_key = os.environ.get("NOTION_API_KEY")
    db_id = os.environ.get("NOTION_DATABASE_ID")

    if not api_key or not db_id:
        print("[SKIP] Notion keys not configured. Skipping Notion write.")
        return

    from notion_client import Client as NotionClient

    notion = NotionClient(auth=api_key)
    date_str = datetime.now().strftime("%Y-%m-%d")

    blocks = []
    for i in range(0, len(digest), 2000):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"text": {"content": digest[i : i + 2000]}}
                ]
            },
        })

    notion.pages.create(
        parent={"database_id": db_id},
        properties={
            "Name": {
                "title": [
                    {"text": {
                        "content": f"ISTS JCC Digest - {date_str}"
                    }}
                ]
            }
        },
        children=blocks[:100],
    )
    print("      Digest written to Notion.")


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
def main():
    print("=" * 60)
    print("  ISTS JCC MONITOR")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    email_method = get_email_method()
    print(f"  Email method: {email_method or 'NOT CONFIGURED'}")
    print(f"  Allowed domains: {', '.join(ALLOWED_DOMAINS)}")
    print(f"  Claude model: {CLAUDE_MODEL}")
    print(f"  Monitoring {len(CTUIL_URLS)} page(s)")
    print("=" * 60)

    session = create_session()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DIGEST_DIR, exist_ok=True)

    # Load tracker
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r") as f:
            processed = json.load(f)
    else:
        processed = []

    # Step 1: Fetch relevant PDF links from all URLs
    pdf_links = []
    for url in CTUIL_URLS:
        pdf_links.extend(fetch_pdf_links(session, url))

    new_pdfs = [p for p in pdf_links if p["url"] not in processed]
    print(f"[2/6] New PDFs to process: {len(new_pdfs)}")

    if not new_pdfs:
        print("      Nothing new. Exiting.")
        return

    # Step 2: Download and extract
    print("[3/6] Downloading and extracting text...")
    extracted = []
    failed = []

    for i, pdf in enumerate(new_pdfs):
        print(f"      [{i+1}/{len(new_pdfs)}] {pdf['text']}")
        print(f"              {pdf['url']}")

        text = download_and_extract(session, pdf["url"])

        if text.startswith("["):
            print(f"              ❌ {text}")
            failed.append({
                "name": pdf["text"],
                "url": pdf["url"],
                "error": text,
            })
        else:
            extracted.append({
                "name": pdf["text"],
                "content": text,
                "url": pdf["url"],
            })
            print(f"              ✅ Extracted {len(text)} characters.")
            processed.append(pdf["url"])

        if i < len(new_pdfs) - 1:
            time.sleep(DOWNLOAD_DELAY)

    print(f"\n      Summary: {len(extracted)} succeeded, "
          f"{len(failed)} failed.")

    if failed:
        print(f"      Failed PDFs (will retry next run):")
        for f_item in failed:
            print(f"        - {f_item['name']}: {f_item['error']}")

    if not extracted:
        print("      No usable text extracted. Exiting.")
        with open(PROCESSED_FILE, "w") as f:
            json.dump(processed, f, indent=2)
        return

    # Step 3: Analyze with Claude
    digest = analyze_with_claude(extracted)

    # Save digest
    date_str = datetime.now().strftime("%Y-%m-%d")
    digest_path = os.path.join(DIGEST_DIR, f"{date_str}.md")
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(digest)
    print(f"      Digest saved: {digest_path}")

    # Step 4: Send email
    send_email_notification(digest, len(extracted), len(failed))

    # Step 5: Notion
    write_to_notion(digest)

    # Update tracker
    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed, f, indent=2)

    print(f"\n[6/6] Done. All steps completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
