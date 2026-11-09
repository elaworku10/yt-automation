import os
import re
import sys
import json
import time

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

LOCAL_FILE = "video_to_upload.mp4"
RETRIABLE = (500, 502, 503, 504)
ADD_SHORTS_TAG = True      # append #Shorts to the description if missing
DONE_PREFIX = "DONE_"      # fallback marker if a file cannot be trashed


# ---------------------------------------------------------------- auth
def creds_from_env(var):
    """Build credentials from a secret holding token JSON. None if empty."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"{var} is not valid JSON: {e}")
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            sys.exit(f"{var}: no refresh token. Regenerate the token.")
    return creds


def load_sources():
    """
    Up to 3 Drive sources. Each source = a folder ID + the account whose
    token can read it.
      FOLDER_ID_1..3   folder IDs
      TOKEN_JSON       main account (YouTube + Drive); default for every folder
      TOKEN_JSON_2/3   optional separate Drive-only tokens for folders 2 and 3
    If you shared folders 2 and 3 with the main account, you only need TOKEN_JSON.
    """
    main = creds_from_env("TOKEN_JSON")
    if main is None:
        sys.exit("TOKEN_JSON secret is empty")

    sources = []
    for i in (1, 2, 3):
        folder = os.environ.get(f"FOLDER_ID_{i}", "").strip()
        if i == 1 and not folder:
            folder = os.environ.get("DRIVE_FOLDER_ID", "").strip()  # old name
        if not folder:
            continue
        creds = creds_from_env(f"TOKEN_JSON_{i}") if i > 1 else None
        creds = creds or main
        sources.append({
            "n": i,
            "folder": folder,
            "drive": build("drive", "v3", credentials=creds),
        })
    if not sources:
        sys.exit("No folder IDs set (FOLDER_ID_1, FOLDER_ID_2, FOLDER_ID_3)")
    return main, sources


# ---------------------------------------------------------------- helpers
def natural_sort_key(filename):
    """massy_2.mp4 comes before massy_10.mp4"""
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r"(\d+)", filename)
    ]


def list_folder(drive, folder):
    files, page_token = [], None
    while True:
        r = drive.files().list(
            q=f"'{folder}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        files.extend(r.get("files", []))
        page_token = r.get("nextPageToken")
        if not page_token:
            return files


def build_queue(sources):
    """All videos from all folders, paired with their .txt, in global order."""
    queue = []
    for s in sources:
        files = list_folder(s["drive"], s["folder"])
        txts = {
            os.path.splitext(f["name"])[0].lower(): f
            for f in files if f["name"].lower().endswith(".txt")
        }
        count = 0
        for f in files:
            name = f["name"]
            if name.lower().endswith(".mp4") and not name.startswith(DONE_PREFIX):
                base = os.path.splitext(name)[0].lower()
                queue.append({"src": s, "video": f, "txt": txts.get(base)})
                count += 1
        print(f"Folder {s['n']}: {count} videos")
    queue.sort(key=lambda q: natural_sort_key(q["video"]["name"]))
    return queue


def read_text(drive, file_id):
    data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    return data.decode("utf-8-sig", errors="replace")


def parse_info(text):
    """
    Supports two txt layouts:
      1) Keyed: TITLE: / DESCRIPTION: / HASHTAGS: / TAGS:
         (the value can be on the same line or on the lines below the key)
      2) Plain: first line = title, everything after = description
    Returns (title, description, tags).
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    key_re = re.compile(r"\s*(TITLE|DESCRIPTION|HASHTAGS|TAGS)\s*:\s*(.*)$", re.I)
    title, description, hashtag_text, tags = "", "", "", []

    if any(key_re.match(l) for l in lines):
        blocks, current = {}, None
        for l in lines:
            m = key_re.match(l)
            if m:
                current = m.group(1).upper()
                blocks[current] = [m.group(2)]
            elif current:
                blocks[current].append(l)
        title = next((l.strip() for l in blocks.get("TITLE", []) if l.strip()), "")
        description = "\n".join(blocks.get("DESCRIPTION", [])).strip()
        hashtag_text = " ".join(blocks.get("HASHTAGS", []))
        raw_tags = " ".join(blocks.get("TAGS", []))
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        if not title and description:
            title = next((l for l in description.split("\n") if l.strip()), "")
    else:
        idx = next((i for i, l in enumerate(lines) if l.strip()), None)
        if idx is not None:
            title = lines[idx].strip()
            description = "\n".join(lines[idx + 1:]).strip()

    # Hashtags: de-duplicate, max 15 (YouTube ignores all hashtags above 60)
    seen, hashtags = set(), []
    for h in re.findall(r"#\w+", hashtag_text):
        if h.lower() not in seen:
            seen.add(h.lower())
            hashtags.append(h)
    hashtags = hashtags[:15]
    if ADD_SHORTS_TAG and "#shorts" not in (description + " ".join(hashtags)).lower():
        hashtags.insert(0, "#Shorts")
    if not tags:
        tags = [h[1:] for h in hashtags]

    # YouTube rules: no < >, title <= 100, description <= 5000, tags <= ~500 chars
    clean = lambda s: s.replace("<", "").replace(">", "")
    title = clean(title).strip()[:100]
    description = clean(description)
    if hashtags:
        description = (description + "\n\n" + " ".join(hashtags)).strip()
    description = description[:5000]

    clean_tags, total = [], 0
    for t in tags:
        t = clean(t)
        if total + len(t) > 450:
            break
        clean_tags.append(t)
        total += len(t)
    return title, description, clean_tags


def download_file(drive, file_id, path):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(path, "wb") as f:
        dl = MediaIoBaseDownload(f, request, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


def retire(drive, f):
    """Trash a file; if not allowed, rename it with DONE_ so it is skipped."""
    try:
        drive.files().update(
            fileId=f["id"], body={"trashed": True}, supportsAllDrives=True
        ).execute()
        return "trashed"
    except HttpError:
        pass
    try:
        drive.files().update(
            fileId=f["id"], body={"name": DONE_PREFIX + f["name"]},
            supportsAllDrives=True,
        ).execute()
        return "renamed"
    except HttpError:
        return None


def upload_video(youtube, path, title, description, tags):
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status in RETRIABLE and retries < 5:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
    return response


# ---------------------------------------------------------------- main
def main():
    main_creds, sources = load_sources()
    youtube = build("youtube", "v3", credentials=main_creds)

    queue = build_queue(sources)
    if not queue:
        print("No videos found.")
        return

    print("\nNext in line:")
    for q in queue[:5]:
        flag = "" if q["txt"] else "  (NO .txt - will be skipped)"
        print(f"  {q['video']['name']}  [folder {q['src']['n']}]{flag}")

    # Take the first video (global order) that has a usable .txt
    chosen = None
    for q in queue:
        if not q["txt"]:
            print(f"Skipping {q['video']['name']}: no matching .txt")
            continue
        text = read_text(q["src"]["drive"], q["txt"]["id"])
        title, description, tags = parse_info(text)
        if not title:
            print(f"Skipping {q['video']['name']}: .txt is empty")
            continue
        chosen = (q, title, description, tags)
        break

    if not chosen:
        sys.exit("No video with a valid .txt file found.")

    q, title, description, tags = chosen
    drive = q["src"]["drive"]
    video = q["video"]
    print(f"\nUploading: {video['name']} (folder {q['src']['n']})")
    print(f"Title: {title}\nTags: {tags}")

    try:
        download_file(drive, video["id"], LOCAL_FILE)
        response = upload_video(youtube, LOCAL_FILE, title, description, tags)
        print(f"Uploaded: https://youtu.be/{response['id']}")
    except HttpError as e:
        if b"quotaExceeded" in (e.content or b""):
            print("YouTube API daily quota exceeded. Try again later.")
        else:
            print(f"Upload error: {e}")
        sys.exit(1)
    finally:
        if os.path.exists(LOCAL_FILE):
            os.remove(LOCAL_FILE)

    # Upload succeeded: remove video (and its txt) from the queue
    result = retire(drive, video)
    if result is None:
        print(f"WARNING: uploaded, but could not trash or rename {video['name']}.")
        print("Remove it manually or it will be uploaded again!")
        sys.exit(1)
    print(f"Video {result}: {video['name']}")

    if not retire(drive, q["txt"]):
        print(f"Note: could not clean up {q['txt']['name']} (harmless).")


if __name__ == "__main__":
    main()
