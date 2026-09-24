"""Reading and sending real email through someone's own IMAP/SMTP mailbox
(Zoho Mail Lite, in Broc's plan) -- stdlib only, matching the plain-Flask,
no-exotic-dependency approach used everywhere else in this app. There is no
local copy of anyone's mail: every inbox view is a live IMAP fetch against
their real mailbox, so there's nothing here to keep in sync or go stale.

NOT YET VERIFIED AGAINST A REAL MAILBOX (2026-09-24) -- written to the
documented IMAP4_SSL/SMTP_SSL/Zoho behavior, but no Zoho account exists yet
to log into. The first real connection attempt (via "Test connection" in
Settings) is the real test of this file, not any deploy or code review.
"""
import email
import imaplib
import smtplib
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr


class EmailAuthError(Exception):
    pass


def _decode(raw):
    if raw is None:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _imap_connect(account):
    try:
        conn = imaplib.IMAP4_SSL(account["imap_host"], account["imap_port"])
        conn.login(account["username"], account["password"])
    except (imaplib.IMAP4.error, OSError) as e:
        raise EmailAuthError(f"IMAP login failed: {e}")
    return conn


def _smtp_connect(account):
    try:
        conn = smtplib.SMTP_SSL(account["smtp_host"], account["smtp_port"])
        conn.login(account["username"], account["password"])
    except (smtplib.SMTPException, OSError) as e:
        raise EmailAuthError(f"SMTP login failed: {e}")
    return conn


def test_connection(account):
    """Raises EmailAuthError with a real reason on failure. Used by the
    Settings "connect" flow so a typo'd password fails loudly right away
    instead of silently sitting broken until someone opens the Email tab."""
    imap = _imap_connect(account)
    imap.logout()
    smtp = _smtp_connect(account)
    smtp.quit()
    return True


def list_messages(account, limit=25, folder="INBOX"):
    conn = _imap_connect(account)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            return []
        uids = data[0].split()
        uids = uids[-limit:][::-1]  # most recent first
        messages = []
        for uid in uids:
            status, msg_data = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            headers = email.message_from_bytes(msg_data[0][1])
            messages.append({
                "uid": uid.decode(),
                "from": _decode(headers.get("From")),
                "subject": _decode(headers.get("Subject")) or "(no subject)",
                "date": headers.get("Date"),
            })
        return messages
    finally:
        conn.logout()


def get_message(account, uid, folder="INBOX"):
    conn = _imap_connect(account)
    try:
        conn.select(folder, readonly=True)
        status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            return None
        msg = email.message_from_bytes(msg_data[0][1])
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
        else:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
        return {
            "uid": uid,
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "subject": _decode(msg.get("Subject")) or "(no subject)",
            "date": msg.get("Date"),
            "body": body,
        }
    finally:
        conn.logout()


def send_message(account, to, subject, body):
    parseaddr(to)  # raises nothing on garbage input, but keeps intent clear
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = account["email_address"]
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    conn = _smtp_connect(account)
    try:
        conn.sendmail(account["email_address"], [to], msg.as_string())
    finally:
        conn.quit()

    # Best-effort: file a copy under Sent so it shows up there too, same as
    # any normal mail client. Not every IMAP server names the folder the
    # same way, and a failure here shouldn't fail the send itself -- the
    # email already went out.
    try:
        imap = _imap_connect(account)
        for folder in ("Sent", "INBOX.Sent", "Sent Items"):
            try:
                imap.append(folder, "", imaplib.Time2Internaldate(__import__("time").time()), msg.as_bytes())
                break
            except imaplib.IMAP4.error:
                continue
        imap.logout()
    except Exception:
        pass
