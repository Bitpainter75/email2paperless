#!/usr/bin/env python3
"""
mail-pdf-bridge - IMAP email fetcher + EML watch folder for Paperless-ngx
"""

import email
import imaplib
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileCreatedEvent
from watchdog.observers import Observer
from weasyprint import HTML

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("mail-pdf-bridge")

# ── Config ────────────────────────────────────────────────────────────────────
IMAP_SERVER      = os.environ.get("IMAP_SERVER", "")
IMAP_PORT        = int(os.environ.get("IMAP_PORT", 993))
IMAP_USERNAME    = os.environ.get("IMAP_USERNAME", "")
IMAP_PASSWORD    = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER      = os.environ.get("IMAP_FOLDER", "INBOX")
IMAP_USE_SSL     = os.environ.get("IMAP_USE_SSL", "true").lower() == "true"
IMAP_FETCH_ALL   = os.environ.get("IMAP_FETCH_ALL", "false").lower() == "true"
IMAP_ENABLED     = bool(IMAP_SERVER and IMAP_USERNAME and IMAP_PASSWORD)

EML_WATCH_DIR    = Path(os.environ.get("EML_WATCH_DIR", "/eml"))
CONSUME_DIR      = Path(os.environ.get("CONSUME_DIR", "/consume"))
SAVE_EMAIL_PDF   = os.environ.get("SAVE_EMAIL_PDF", "true").lower() == "true"
SAVE_ATTACHMENTS = os.environ.get("SAVE_ATTACHMENTS", "true").lower() == "true"
ATTACHMENT_TYPES = os.environ.get("ATTACHMENT_TYPES", ".pdf,.png,.jpg,.jpeg,.tiff")
MARK_SEEN        = os.environ.get("MARK_SEEN", "true").lower() == "true"
MOVE_TO_FOLDER   = os.environ.get("MOVE_TO_FOLDER", "")
DELETE_AFTER     = os.environ.get("DELETE_AFTER", "false").lower() == "true"
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", 300))
DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() == "true"
BLOCK_EXTERNAL_URLS = os.environ.get("BLOCK_EXTERNAL_URLS", "false").lower() == "true"

ALLOWED_EXTENSIONS = {ext.strip().lower() for ext in ATTACHMENT_TYPES.split(",")}



# ── Helpers ───────────────────────────────────────────────────────────────────

def decode_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r'[^\w\s\-_.,()[\]#@+]', "", name, flags=re.UNICODE)
    name = re.sub(r'[ _]+', " ", name).strip() 
    return name[:max_len] or "unnamed"


def timestamp_prefix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def decode_part(payload: bytes, part_charset: str | None) -> str:
    """
    Decode raw bytes to str, trying multiple charsets in order:
    1. Charset declared in the part header
    2. Charset from HTML <meta charset> tag
    3. utf-8
    4. latin-1 (covers windows-1252 / iso-8859-1, common in German emails)
    """
    candidates = []
    if part_charset:
        candidates.append(part_charset)

    # Detect charset from HTML meta tag
    meta_match = re.search(rb'(?i)<meta[^>]+charset=[\x22\x27\s]*([\w-]+)', payload)
    if meta_match:
        detected = meta_match.group(1).decode("ascii", errors="ignore")
        if detected not in candidates:
            candidates.append(detected)

    for cs in ("utf-8", "latin-1"):
        if cs not in candidates:
            candidates.append(cs)

    for charset in candidates:
        try:
            return payload.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue

    return payload.decode("latin-1", errors="replace")


def format_address(raw: str) -> str:
    parts = []
    for name, addr in getaddresses([raw]):
        name = name.strip()
        addr = addr.strip()
        if name and addr and name.lower() != addr.lower():
            parts.append(f"{name} &lt;{addr}&gt;")
        elif addr:
            parts.append(addr)
        elif name:
            parts.append(name)
    return ", ".join(parts) if parts else raw


def format_date_de(raw: str) -> str:
    MONTHS_DE = {
        1: "Januar", 2: "Februar", 3: "März", 4: "April",
        5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
        9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
    }
    WEEKDAYS_DE = {
        0: "Montag", 1: "Dienstag", 2: "Mittwoch", 3: "Donnerstag",
        4: "Freitag", 5: "Samstag", 6: "Sonntag",
    }
    try:
        dt = parsedate_to_datetime(raw)
        tz_offset = dt.utcoffset()
        if tz_offset is not None:
            total_minutes = int(tz_offset.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            hours, mins = divmod(abs(total_minutes), 60)
            tz_str = f"UTC{sign}{hours}" if mins == 0 else f"UTC{sign}{hours}:{mins:02d}"
        else:
            tz_str = "UTC"
        weekday = WEEKDAYS_DE[dt.weekday()]
        month = MONTHS_DE[dt.month]
        return f"{weekday}, {dt.day}. {month} {dt.year}, {dt.strftime('%H:%M')} Uhr ({tz_str})"
    except Exception:
        return raw


def collect_cid_images(msg: email.message.Message) -> dict:
    import base64
    cid_map = {}
    for part in msg.walk():
        cid = part.get("Content-ID", "")
        if not cid:
            continue
        ct = part.get_content_type()
        if not ct.startswith("image/"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        cid_clean = cid.strip("<>")
        b64 = base64.b64encode(payload).decode("ascii")
        cid_map[cid_clean] = f"data:{ct};base64,{b64}"
    return cid_map


def inline_cid_images(html: str, cid_map: dict) -> str:
    for cid, data_uri in cid_map.items():
        html = html.replace(f"cid:{cid}", data_uri)
    return html


def get_email_body_html(msg: email.message.Message) -> str | None:
    html_part = None
    html_cs   = None
    text_part = None
    text_cs   = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            if ct == "text/html" and html_part is None:
                html_part = part.get_payload(decode=True)
                html_cs   = part.get_content_charset()
            elif ct == "text/plain" and text_part is None:
                text_part = part.get_payload(decode=True)
                text_cs   = part.get_content_charset()
    else:
        payload = msg.get_payload(decode=True)
        if msg.get_content_type() == "text/html":
            html_part = payload
            html_cs   = msg.get_content_charset()
        else:
            text_part = payload
            text_cs   = msg.get_content_charset()

    if html_part:
        html_str = decode_part(html_part, html_cs)
        cid_map = collect_cid_images(msg)
        if cid_map:
            log.debug("Inlining %d cid: image(s) into HTML.", len(cid_map))
            html_str = inline_cid_images(html_str, cid_map)
        return html_str

    if text_part:
        text = decode_part(text_part, text_cs)
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<html><body><pre style='font-family:sans-serif'>{escaped}</pre></body></html>"

    return None


def build_email_html(msg: email.message.Message) -> str:
    subject = decode_str(msg.get("Subject", "(no subject)"))
    from_   = format_address(decode_str(msg.get("From", "")))
    to_     = format_address(decode_str(msg.get("To", "")))
    date_   = format_date_de(decode_str(msg.get("Date", "")))

    body = get_email_body_html(msg) or "<p><em>(no body)</em></p>"
    body_inner = re.sub(
        r"(?i)</?html[^>]*>|</?body[^>]*>|</?head[^>]*>.*?</head>",
        "", body, flags=re.DOTALL
    )
    # Fix image sizing: read actual pixel dimensions and set explicit style
    # so WeasyPrint renders at natural size, capped at page width
    def _fix_img_size(m: re.Match) -> str:
        import base64, struct, io
        tag = m.group(0)

        # Extract src
        src_m = re.search(r'src=["\'\']([^"\'\'>]+)["\'\']', tag, re.IGNORECASE)
        if not src_m:
            return tag

        src = src_m.group(1)
        w = h = None

        # 1. Respect explicit HTML width/height attributes — sender's intent
        w_attr = re.search(r'width=["\'\']?(\d+)["\'\']?', tag, re.IGNORECASE)
        h_attr = re.search(r'height=["\'\']?(\d+)["\'\']?', tag, re.IGNORECASE)
        if w_attr:
            w = int(w_attr.group(1))
        if h_attr:
            h = int(h_attr.group(1))

        # 2. Only read from image data if NO size was specified in HTML
        if w is None and src.startswith("data:image/"):
            try:
                b64data = src.split(",", 1)[1]
                raw = base64.b64decode(b64data)
                if raw[:4] == b"\x89PNG":
                    w, h = struct.unpack(">II", raw[16:24])
                elif raw[:2] == b"\xff\xd8":
                    i = 2
                    while i < len(raw) - 8:
                        if raw[i] != 0xff:
                            break
                        marker = raw[i+1]
                        length = struct.unpack(">H", raw[i+2:i+4])[0]
                        if marker in (0xC0, 0xC1, 0xC2):
                            h = struct.unpack(">H", raw[i+5:i+7])[0]
                            w = struct.unpack(">H", raw[i+7:i+9])[0]
                            break
                        i += 2 + length
                elif raw[:3] == b"GIF":
                    w, h = struct.unpack("<HH", raw[6:10])
                elif raw[8:12] == b"WEBP":
                    w = struct.unpack("<H", raw[26:28])[0] + 1
                    h = struct.unpack("<H", raw[28:30])[0] + 1
            except Exception:
                pass

        # Remove existing width/height attributes — we'll set via style instead
        tag = re.sub(r'\s+width=["\'\']?[\d]+["\'\']?', '', tag, flags=re.IGNORECASE)
        tag = re.sub(r'\s+height=["\'\']?[\d]+["\'\']?', '', tag, flags=re.IGNORECASE)

        # Set explicit pixel size capped at 540px (A4 minus margins)
        MAX_W = 540
        if w and h:
            if w > MAX_W:
                h = int(h * MAX_W / w)
                w = MAX_W
            tag = re.sub(r'\s+style=["\'\'][^"\'\'>]*["\'\']', '', tag, flags=re.IGNORECASE)
            tag = tag[:-1] + f' style="width:{w}px;height:{h}px">'
        elif w:
            capped = min(w, MAX_W)
            tag = tag[:-1] + f' style="width:{capped}px;height:auto">'
        else:
            # No size info at all — constrain to page width only
            tag = tag[:-1] + ' style="max-width:540px;height:auto">'

        return tag

    body_inner = re.sub(r'(?i)<img\b[^>]+>', _fix_img_size, body_inner)
    body_inner = re.sub(r'(?i)(<table\b[^>]*?)\s+width=["\'\']?[\d%]+["\'\']?', r'\1', body_inner)

    header_html = f"""
    <div style="border-bottom:1px solid #ccc;margin-bottom:1em;padding-bottom:.5em;
                font-family:sans-serif;font-size:13px;color:#444;">
        <div><strong>Von:</strong> {from_}</div>
        <div><strong>An:</strong> {to_}</div>
        <div><strong>Datum:</strong> {date_}</div>
        <div><strong>Betreff:</strong> {subject}</div>
    </div>
    """

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>
  @page {{ margin: 1.5cm 1.5cm 1.5cm 1.5cm; }}
  body {{ font-family: sans-serif; font-size: 12px; margin: 0; }}
  img  {{ max-width: 100%; height: auto; display: inline-block; }}
  pre  {{ white-space: pre-wrap; word-break: break-word; font-size: 11px; }}
  table {{ max-width: 100% !important; }}
  td, th {{ font-size: 12px; }}
</style>
</head>
<body>
{header_html}
{body_inner}
</body></html>"""


def _local_only_fetcher(url: str, timeout=10, ssl_context=None):
    """
    Custom WeasyPrint URL fetcher that blocks all external HTTP/HTTPS requests.
    Only data: URIs and file: URIs are allowed (e.g. embedded base64 images).
    This prevents tracking pixels and external resource loading.
    """
    from weasyprint import default_url_fetcher
    if url.startswith("data:") or url.startswith("file:"):
        return default_url_fetcher(url)
    log.debug("Blocked external URL during PDF conversion: %s", url)
    # Return empty content instead of raising, so conversion continues cleanly
    return {"string": b"", "mime_type": "text/plain"}


def convert_html_to_pdf(html: str, output_path: Path) -> bool:
    try:
        fetcher = _local_only_fetcher if BLOCK_EXTERNAL_URLS else None
        HTML(string=html, url_fetcher=fetcher).write_pdf(str(output_path))
        return True
    except Exception as exc:
        log.error("WeasyPrint error: %s", exc)
        return False


def extract_attachments(msg: email.message.Message, dest_dir: Path) -> list[Path]:
    saved = []
    for part in msg.walk():
        ct = part.get_content_type()
        cd = part.get("Content-Disposition", "")

        # Skip the email body parts (text/html and text/plain without a filename)
        filename = decode_str(part.get_filename() or "")
        if not filename:
            continue

        # Skip multipart containers
        if ct.startswith("multipart/"):
            continue

        # Skip inline images that are embedded in the HTML body (have Content-ID)
        if part.get("Content-ID") and "attachment" not in cd:
            continue

        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            log.info("  Skipping attachment (type not allowed): %s", filename)
            continue
        safe_name = safe_filename(Path(filename).stem) + ext
        out_path = dest_dir / safe_name
        counter = 1
        while out_path.exists():
            out_path = dest_dir / f"{safe_filename(Path(filename).stem)}_{counter}{ext}"
            counter += 1
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if not DRY_RUN:
            out_path.write_bytes(payload)
        log.info("  Attachment saved: %s (%d bytes)", out_path.name, len(payload))
        saved.append(out_path)
    return saved


# ── Core processing ───────────────────────────────────────────────────────────

def process_msg_object(msg: email.message.Message, prefix: str) -> list[Path]:
    files_to_copy: list[Path] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        if SAVE_EMAIL_PDF:
            html = build_email_html(msg)
            pdf_path = tmp_path / f"{prefix}.pdf"
            if convert_html_to_pdf(html, pdf_path):
                files_to_copy.append(pdf_path)
                log.info("  Email PDF created: %s", pdf_path.name)
            else:
                log.warning("  Email PDF conversion failed.")

        if SAVE_ATTACHMENTS:
            saved = extract_attachments(msg, tmp_path)
            for att in saved:
                renamed = tmp_path / f"{prefix}_{att.name}"
                att.rename(renamed)
                files_to_copy.append(renamed)

        copied: list[Path] = []
        if not DRY_RUN:
            for f in files_to_copy:
                dest = CONSUME_DIR / f.name
                shutil.copy2(f, dest)
                log.info("  → Consume: %s", dest)
                copied.append(dest)
        else:
            for f in files_to_copy:
                log.info("  [DRY RUN] would copy: %s → %s", f.name, CONSUME_DIR)
            copied = files_to_copy

    return copied


# ── IMAP ──────────────────────────────────────────────────────────────────────

def process_imap_message(imap, uid: bytes) -> None:
    _, data = imap.uid("FETCH", uid, "(RFC822)")
    raw = data[0][1]
    msg = email.message_from_bytes(raw)
    subject = decode_str(msg.get("Subject", "(no subject)"))
    log.info("IMAP: %s", subject)
    prefix = timestamp_prefix() + "_" + safe_filename(subject, 50)
    copied = process_msg_object(msg, prefix)

    if MARK_SEEN and not DRY_RUN:
        imap.uid("STORE", uid, "+FLAGS", "\\Seen")

    if MOVE_TO_FOLDER and not DRY_RUN:
        imap.uid("COPY", uid, MOVE_TO_FOLDER)
        imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
        imap.expunge()
        log.info("  Moved to folder: %s", MOVE_TO_FOLDER)
    elif DELETE_AFTER and not DRY_RUN:
        if copied:
            imap.uid("STORE", uid, "+FLAGS", "\\Deleted")
            imap.expunge()
            log.info("  Deleted from server after successful processing.")
        else:
            log.warning("  DELETE_AFTER set but no files produced — mail kept on server.")


def run_imap() -> None:
    if not IMAP_ENABLED:
        return
    log.info("IMAP: connecting to %s:%d ...", IMAP_SERVER, IMAP_PORT)
    try:
        imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) if IMAP_USE_SSL else imaplib.IMAP4(IMAP_SERVER, IMAP_PORT)
        imap.login(IMAP_USERNAME, IMAP_PASSWORD)
        imap.select(IMAP_FOLDER)
        search_criterion = "ALL" if IMAP_FETCH_ALL else "UNSEEN"
        _, uids = imap.uid("SEARCH", None, search_criterion)
        uid_list = uids[0].split()
        if not uid_list:
            log.info("IMAP: no messages found (%s) in %s.", search_criterion, IMAP_FOLDER)
        else:
            log.info("IMAP: %d message(s) found (%s).", len(uid_list), search_criterion)
            for uid in uid_list:
                try:
                    process_imap_message(imap, uid)
                except Exception as exc:
                    log.error("IMAP: error processing UID %s: %s", uid, exc)
        imap.logout()
    except Exception as exc:
        log.error("IMAP: connection error: %s", exc)


# ── EML watch folder ──────────────────────────────────────────────────────────

def process_eml_file(eml_path: Path) -> None:
    log.info("EML: processing %s", eml_path.name)
    try:
        raw = eml_path.read_bytes()
        msg = email.message_from_bytes(raw)
        subject = decode_str(msg.get("Subject", eml_path.stem))
        prefix = timestamp_prefix() + "_" + safe_filename(subject, 50)
        copied = process_msg_object(msg, prefix)

        if copied:
            if DELETE_AFTER and not DRY_RUN:
                eml_path.unlink()
                log.info("  EML file deleted: %s", eml_path.name)
            elif not DRY_RUN:
                done_dir = EML_WATCH_DIR / "processed"
                done_dir.mkdir(exist_ok=True)
                shutil.move(str(eml_path), done_dir / eml_path.name)
                log.info("  EML file moved to processed/: %s", eml_path.name)
            else:
                log.info("  [DRY RUN] EML file would be moved/deleted: %s", eml_path.name)
        else:
            log.warning("  No files produced — EML file kept: %s", eml_path.name)
    except Exception as exc:
        log.error("EML: error processing %s: %s", eml_path.name, exc)


def run_eml_watch() -> None:
    eml_files = sorted(EML_WATCH_DIR.glob("*.eml"))
    if eml_files:
        log.info("EML watch: %d file(s) found at startup.", len(eml_files))
        for eml_path in eml_files:
            process_eml_file(eml_path)


class EmlHandler(FileSystemEventHandler):
    def on_created(self, event: FileCreatedEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".eml":
            return
        time.sleep(1)
        process_eml_file(path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("mail-pdf-bridge started (interval: %ds, dry_run: %s)", INTERVAL_SECONDS, DRY_RUN)
    log.info("IMAP enabled: %s | EML watch: %s", IMAP_ENABLED, EML_WATCH_DIR)

    CONSUME_DIR.mkdir(parents=True, exist_ok=True)
    EML_WATCH_DIR.mkdir(parents=True, exist_ok=True)

    run_eml_watch()

    observer = Observer()
    observer.schedule(EmlHandler(), str(EML_WATCH_DIR), recursive=False)
    observer.start()
    log.info("EML watcher active on %s", EML_WATCH_DIR)

    try:
        while True:
            run_imap()
            log.info("Sleeping %d seconds until next IMAP check ...", INTERVAL_SECONDS)
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
