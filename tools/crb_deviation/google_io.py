"""
Google access for the deviation back-test: sign in as a person, read the
intake sheet with its links, and download the bureau PDFs.

Runs as whoever signs in (read-only scopes), so it sees exactly the sheet and
Drive files that person can open. The sign-in is cached in token.json beside
this file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly",
          "https://www.googleapis.com/auth/drive.readonly"]
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets/"
DRIVE = "https://www.googleapis.com/drive/v3/files/"
HEADER_SCAN_ROWS = 15

# Header matching, as the intake Apps Script does it: exact names first
# (compared without case, spaces or punctuation), then a looser rule.
CUSTOMER = {"customer", "customername", "name", "applicant", "applicantname"}
CRB = {"crb", "crblink", "crbreport", "crbreportlink", "crburl", "crbpdf",
       "bureaureport", "creditreport"}


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def loose_crb(k: str) -> bool:
    return (bool(re.search(r"crb|bureau|credit", k)) and bool(re.search(r"link|report|url|pdf|file", k))
            and not re.search(r"from|band|value|total|number|score", k))


def session(client_secret: Path, token: Path):
    """An authorised requests session, signing in through the browser when needed."""
    from google.auth.transport.requests import AuthorizedSession, Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not client_secret.exists():
            raise SystemExit(f"No OAuth client file at {client_secret}. See README.md, 'One-time setup'.")
        creds = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES).run_local_server(port=0)
        token.write_text(creds.to_json())
    return AuthorizedSession(creds)


def spreadsheet_id(text: str) -> str:
    m = re.search(r"/spreadsheets/d/([\w-]+)", text)
    return m.group(1) if m else text.strip()


def _check(res, what: str):
    if res.status_code != 200:
        try:
            msg = res.json()["error"]["message"]
        except Exception:
            msg = res.text[:300]
        raise RuntimeError(f"{what}: HTTP {res.status_code}: {msg}")
    return res


def cell_links(v: dict) -> List[str]:
    """Every link in a cell: smart chips, hyperlinks, =HYPERLINK, linked text, pasted URLs."""
    found = [((r.get("chip") or {}).get("richLinkProperties") or {}).get("uri")
             for r in v.get("chipRuns") or []]
    found.append(v.get("hyperlink"))
    found += [((r.get("format") or {}).get("link") or {}).get("uri") for r in v.get("textFormatRuns") or []]
    f = re.search(r'HYPERLINK\(\s*"([^"]+)"', (v.get("userEnteredValue") or {}).get("formulaValue") or "", re.I)
    if f:
        found.append(f.group(1))
    found += re.findall(r"https?://[^\s,;\"'<>]+", v.get("formattedValue") or "")
    out: List[str] = []
    for link in found:
        if link and link not in out:
            out.append(link)
    return out


def read_tab(sess, sheet_id: str, tab: Optional[str]) -> Tuple[str, List[List[dict]]]:
    """(tab title, rows), each row a list of {"text", "links"} cells."""
    meta = _check(sess.get(SHEETS + sheet_id, params={"fields": "sheets.properties.title"}),
                  "Could not open the spreadsheet").json()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    title = tab or titles[0]
    if title not in titles:
        raise SystemExit(f'No tab named "{title}". Tabs: {", ".join(titles)}.')
    fields = ("sheets.data.rowData.values(formattedValue,hyperlink,userEnteredValue.formulaValue,"
              "chipRuns.chip.richLinkProperties.uri,textFormatRuns.format.link.uri)")
    data = _check(sess.get(SHEETS + sheet_id, params={
        "ranges": "'" + title.replace("'", "''") + "'", "includeGridData": "true", "fields": fields,
    }), "Could not read the tab").json()
    grid = ((data.get("sheets") or [{}])[0].get("data") or [{}])[0].get("rowData") or []
    rows = [[{"text": (v.get("formattedValue") or "").strip(), "links": cell_links(v)}
             for v in r.get("values") or []] for r in grid]
    return title, rows


def find_columns(rows: List[List[dict]], crb_header: Optional[str] = None) -> Tuple[int, Dict[str, int]]:
    """(header row index, {field: column}) for the first top row with a CRB column.

    Fields: crb (required), customer, plus band, status and application id when
    present, carried into the output for reference.
    """
    for r, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        keys = [key(c["text"]) for c in row]
        cols: Dict[str, int] = {}

        def claim(field: str, test) -> None:
            for i, k in enumerate(keys):
                if field not in cols and k and i not in cols.values() and test(k):
                    cols[field] = i

        if crb_header:
            claim("crb", lambda k: k == key(crb_header))
        else:
            claim("crb", lambda k: k in CRB)
            claim("crb", loose_crb)
        if "crb" not in cols:
            continue
        claim("customer", lambda k: k in CUSTOMER)
        claim("application_id", lambda k: k in {"applicationid", "appid", "applicationno"})
        claim("sheet_band", lambda k: "band" in k)
        claim("sheet_status", lambda k: k in {"status", "decision", "creditdecision"}
              or (bool(re.search(r"status|decision", k)) and bool(re.search(r"sign|credit|risk|underwrit", k))))
        return r, cols
    headers = [c["text"] for row in rows[:HEADER_SCAN_ROWS] for c in row if c["text"]]
    raise SystemExit("No CRB link column in the top {} rows. Pass --crb-column with its header. "
                     "Headers seen: {}".format(HEADER_SCAN_ROWS, ", ".join(headers[:60])))


def drive_id(link: str) -> Optional[str]:
    """The file ID of a Drive or Docs link; None for anything else, folders included."""
    if "/folders/" in link or not re.search(r"(drive|docs)\.google\.com", link):
        return None
    m = re.search(r"/d/([\w-]{20,})", link) or re.search(r"[?&]id=([\w-]{20,})", link)
    return m.group(1) if m else None


class CannotAssess(Exception):
    """Why a row could not be assessed: a short reason, the specifics, and what to do."""

    def __init__(self, reason: str, detail: str, action: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.action = reason, detail, action


def signed_in_as(sess) -> str:
    res = sess.get("https://www.googleapis.com/drive/v3/about", params={"fields": "user(emailAddress)"})
    return res.json().get("user", {}).get("emailAddress", "the signed-in account") if res.ok else "the signed-in account"


_KINDS = {"application/vnd.google-apps.document": "a Google Doc",
          "application/vnd.google-apps.spreadsheet": "a Google Sheet",
          "application/vnd.google-apps.presentation": "a Google Slides file",
          "application/msword": "a Word document",
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "a Word document"}
_PDF_TYPES = {"application/pdf", "application/octet-stream", "binary/octet-stream", ""}


def _kind(mime: str) -> str:
    if mime in _KINDS:
        return _KINDS[mime]
    if mime.startswith("image/"):
        return f"an image ({mime.split('/', 1)[1].upper()})"
    return f"a '{mime}' file"


def _google_error(res) -> CannotAssess:
    try:
        err = res.json()["error"]
        msg, reasons = err.get("message", ""), {e.get("reason") for e in err.get("errors", [])}
    except Exception:
        msg, reasons = res.text[:200], set()
    if res.status_code == 429 or reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return CannotAssess("Google was busy", f"Google Drive asked the tool to slow down ({msg}).",
                            "Run the report again. Files already downloaded are reused.")
    return CannotAssess("Google Drive error", f"HTTP {res.status_code}: {msg}",
                        "Run the report again. If it repeats, open the CRB link to check the file.")


def download(sess, file_id: str, cache: Path, account: str, _hops: int = 0) -> Tuple[bytes, str]:
    """(PDF bytes, file name), cached by ID so a rerun does not download again.

    Raises CannotAssess saying exactly why the file could not be used.
    """
    pdf, meta = cache / f"{file_id}.pdf", cache / f"{file_id}.json"
    if pdf.exists():
        name = json.loads(meta.read_text()).get("name", "") if meta.exists() else ""
        return pdf.read_bytes(), name
    res = sess.get(DRIVE + quote(file_id), params={
        "fields": "name,mimeType,capabilities/canDownload,shortcutDetails", "supportsAllDrives": "true"})
    if res.status_code == 404:
        raise CannotAssess(
            "No access to the CRB file",
            f"Google Drive says this file doesn't exist or isn't shared with {account}, "
            "the account this report was run as.",
            f"Open the CRB link while signed in as {account}. If Google asks for access, request it, "
            f"or ask whoever uploaded the CRB to share it with {account}. Then run the report again.")
    if res.status_code != 200:
        raise _google_error(res)
    m = res.json()
    name, mime = m.get("name", ""), m.get("mimeType", "")
    target = (m.get("shortcutDetails") or {}).get("targetId")
    if target and _hops == 0:
        return download(sess, target, cache, account, 1)
    if mime == "application/vnd.google-apps.folder":
        raise CannotAssess("CRB link is a folder", f"The link opens the folder '{name}', not a file.",
                           "Replace it with the link to the CRB PDF itself.")
    if mime not in _PDF_TYPES:
        raise CannotAssess("CRB file is not a PDF", f"The linked file '{name}' is {_kind(mime)}, not a PDF.",
                           "Link the CRB report PDF as downloaded from the bureau. "
                           "A photo, scan or retyped copy can't be read.")
    if (m.get("capabilities") or {}).get("canDownload") is False:
        raise CannotAssess("Download turned off", f"The owner of '{name}' has turned off downloading for viewers.",
                           f"Ask the owner to allow downloads, or to give {account} edit access.")
    media = sess.get(DRIVE + quote(file_id), params={"alt": "media", "supportsAllDrives": "true"})
    if media.status_code != 200:
        raise _google_error(media)
    if not media.content.startswith(b"%PDF"):
        raise CannotAssess("CRB file is not a PDF", f"'{name}' is named or labelled as a PDF but isn't one.",
                           "Link the CRB report PDF as downloaded from the bureau.")
    cache.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(media.content)
    meta.write_text(json.dumps({"name": name}))
    return media.content, name
