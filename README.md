# mail-pdf-bridge

A lightweight Docker container that bridges your email inbox and [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx), or [Papra](https://github.com/papra-hq/papra), or local E-Mail Archive.

It fetches emails via IMAP and/or watches a local folder for `.eml` files, then:
- converts the email body to a **PDF** (with sender, recipient, date, and subject header)
- extracts **attachments** as individual files
- drops everything into your **Paperless consume folder** for automatic OCR and archiving

No Gotenberg. No Tika. No Chromium. Just Python + [WeasyPrint](https://weasyprint.org/).

---

## Features

- **Dual input**: IMAP polling + local `.eml` watch folder (both can run simultaneously)
- **Instant processing** of dropped `.eml` files via filesystem watcher (`inotify`), no polling delay
- **Separate files**: email body PDF and attachments are saved independently so Paperless indexes them individually
- **Configurable attachment types**: only forward the file types you actually want
- **Clean filenames**: emojis and special characters are stripped, umlauts are preserved
- **Flexible post-processing**: mark as seen, move to IMAP folder, or delete after successful processing
- **Safe deletion**: emails are only deleted if at least one file was successfully written to the consume folder
- **Backlog import**: optional `IMAP_FETCH_ALL` mode to process all messages, not just unread ones
- **Dry-run mode**: logs what would happen without writing or deleting anything
- Startup scan: any `.eml` files already in the watch folder are processed immediately on container start

---

## Requirements

- Docker + Docker Compose
- A running [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) instance with an accessible consume folder
- An IMAP mailbox (optional — the container works without IMAP if you only use the watch folder)

> **Gotenberg and Tika are not required.** If you were previously running them only for email processing, you can remove them from your Paperless stack.

---

## Quick Start

```bash
git clone https://github.com/Bitpainter75/mail-pdf-bridge.git
cd mail-pdf-bridge
```

Edit `docker-compose.yml` with your settings, then:

```bash
docker compose build
docker compose up -d
```

To test without writing anything:

```bash
# Set in docker-compose.yml:
DRY_RUN: "true"
```

---

## Configuration

All configuration is done via environment variables in `docker-compose.yml`.

### IMAP

| Variable | Default | Description |
|---|---|---|
| `IMAP_SERVER` | *(empty)* | IMAP hostname — leave empty to disable IMAP |
| `IMAP_PORT` | `993` | IMAP port |
| `IMAP_USERNAME` | *(empty)* | IMAP login |
| `IMAP_PASSWORD` | *(empty)* | IMAP password |
| `IMAP_FOLDER` | `INBOX` | Folder to monitor (e.g. `Paperless`) |
| `IMAP_USE_SSL` | `true` | Use SSL/TLS |
| `IMAP_FETCH_ALL` | `false` | `true` = fetch all messages, not just unread |

### Behaviour

| Variable | Default | Description |
|---|---|---|
| `SAVE_EMAIL_PDF` | `true` | Convert email body to PDF |
| `SAVE_ATTACHMENTS` | `true` | Extract attachments separately |
| `ATTACHMENT_TYPES` | `.pdf,.png,.jpg,.jpeg,.tiff` | Comma-separated list of allowed extensions |
| `MARK_SEEN` | `true` | Mark emails as read after processing (IMAP only) |
| `MOVE_TO_FOLDER` | *(empty)* | Move to this IMAP folder after processing (takes priority over `DELETE_AFTER`) |
| `DELETE_AFTER` | `false` | Delete email/file after successful processing |
| `INTERVAL_SECONDS` | `300` | IMAP polling interval in seconds |
| `DRY_RUN` | `false` | Log only — nothing is written or deleted |

### Paths

| Variable | Default | Description |
|---|---|---|
| `CONSUME_DIR` | `/consume` | Paperless consume folder (mount your actual path here) |
| `EML_WATCH_DIR` | `/eml` | Local folder to watch for `.eml` files |

---

## Volumes

```yaml
volumes:
  - /path/to/paperless/consume:/consume   # Paperless consume folder
  - /path/to/eml/inbox:/eml               # Drop .eml files here
```

---

## How the EML Watch Folder Works

Drop any `.eml` file into the mounted `/eml` folder — the container detects it instantly via `inotify` and processes it within ~1 second.

After successful processing:
- `DELETE_AFTER: false` (default) → file is moved to `/eml/processed/`
- `DELETE_AFTER: true` → file is deleted
- On error → file stays untouched in `/eml`

You can export `.eml` files from most email clients (Thunderbird, Apple Mail, Outlook) via drag & drop or "Save As".

---

## Output Filenames

Files are named with a timestamp prefix and the email subject:

```
20260525_143022_Invoice_April_2026_email.pdf
20260525_143022_Invoice_April_2026_invoice.pdf
```

Emojis and special characters are stripped from filenames. Standard characters including umlauts (ä, ö, ü) are preserved.

---

## Paperless Integration Tips

- Point a dedicated IMAP folder (e.g. `Paperless`) at this container — drag emails into that folder from your mail client to queue them
- Use Paperless [tags and correspondents](https://docs.paperless-ngx.com/usage/#basic-usage) to auto-classify documents after they land in the consume folder
- If you only process PDFs and images, you can remove **Gotenberg** and **Tika** from your Paperless `docker-compose.yml` entirely

---

## Architecture

```
IMAP inbox  ──►┐
               ├──► mail-pdf-bridge ──► /consume ──► Paperless-ngx
.eml folder ──►┘
```

The container runs a single Python process:
- A background thread watches the `/eml` folder via `inotify`
- The main loop polls IMAP at the configured interval
- Both paths share the same processing pipeline (PDF conversion + attachment extraction)

---

## License

MIT
