import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

PAGE_ID = "1055198774653024"
PAGE_NAME = "Casual Misconduct"
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v24.0")
REPO = "SmokeyRiverStudio/maplesap"

CAPTIONS = {
    1: "Disappointed expectations, delivered fresh.",
    2: "Two brain cells. Neither one is supervising.",
    3: "Some days the third chin gets a vote.",
    4: "A rare moment of silence. Enjoy it while it lasts.",
    5: "The bar was on the ground and somehow we still tripped over it.",
    6: "Not just wrong. Fascinatingly wrong.",
    7: "Adult supervision has left the building.",
    8: "Should have come with a warning label.",
    9: "Not a mistake. A whole personality choice.",
}

ALLOWED_DESTINATIONS = {"facebook", "instagram"}

def graph(method, path, token, **kwargs):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    response = requests.request(method, url, timeout=180, **kwargs)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:1000]}
    if not response.ok or data.get("error"):
        raise RuntimeError(f"Meta API {method} {path} failed ({response.status_code}): {json.dumps(data)}")
    return data

def resolve_page(user_token):
    data = graph(
        "GET",
        "me/accounts",
        user_token,
        params={
            "fields": "id,name,access_token,tasks,instagram_business_account",
            "limit": "100",
            "access_token": user_token,
        },
    )
    matches = [p for p in data.get("data", []) if str(p.get("id")) == PAGE_ID and p.get("name") == PAGE_NAME]
    if len(matches) != 1:
        visible = [{"id": p.get("id"), "name": p.get("name")} for p in data.get("data", [])]
        raise RuntimeError(f"Exact Casual Misconduct Page guard failed. Visible Pages: {visible}")
    page = matches[0]
    required = {"CREATE_CONTENT"}
    tasks = set(page.get("tasks") or [])
    if not required.issubset(tasks):
        raise RuntimeError(f"Casual Misconduct Page is missing required tasks. Returned: {sorted(tasks)}")
    return page

def publish_facebook_reel(page, video_path, caption):
    token = page["access_token"]
    start = graph(
        "POST",
        f"{PAGE_ID}/video_reels",
        token,
        data={"upload_phase": "start", "access_token": token},
    )
    video_id = start["video_id"]
    upload_url = start["upload_url"]
    size = video_path.stat().st_size
    with video_path.open("rb") as handle:
        response = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(size),
                "Content-Type": "application/octet-stream",
            },
            data=handle,
            timeout=900,
        )
    try:
        upload = response.json()
    except Exception:
        upload = {"raw": response.text[:1000]}
    if not response.ok or upload.get("error"):
        raise RuntimeError(f"Facebook Reel upload failed ({response.status_code}): {json.dumps(upload)}")
    finish = graph(
        "POST",
        f"{PAGE_ID}/video_reels",
        token,
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": token,
        },
    )
    return {"video_id": video_id, "finish": finish}

def publish_instagram_reel(page, video_url, caption):
    instagram = page.get("instagram_business_account") or {}
    instagram_id = instagram.get("id")
    if not instagram_id:
        raise RuntimeError("Casual Misconduct Page has no linked Instagram Business account")
    token = page["access_token"]
    container = graph(
        "POST",
        f"{instagram_id}/media",
        token,
        data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        },
    )
    container_id = container["id"]
    for _ in range(60):
        status = graph(
            "GET",
            container_id,
            token,
            params={"fields": "status_code,status", "access_token": token},
        )
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram container {container_id} reached {code}: {status}")
        time.sleep(5)
    else:
        raise RuntimeError(f"Instagram container {container_id} was not ready after 5 minutes")
    published = graph(
        "POST",
        f"{instagram_id}/media_publish",
        token,
        data={"creation_id": container_id, "access_token": token},
    )
    return {"container_id": container_id, "media_id": published.get("id")}

def main():
    seed = int(os.environ["SEED_NUMBER"])
    destinations = {
        item.strip().lower()
        for item in os.environ.get("DESTINATIONS", "facebook").split(",")
        if item.strip()
    }
    unknown_destinations = destinations - ALLOWED_DESTINATIONS
    if unknown_destinations:
        raise RuntimeError(f"Unknown destination(s): {sorted(unknown_destinations)}")
    if not destinations:
        raise RuntimeError("DESTINATIONS must include at least one destination")
    user_token = os.environ.get("FB_ACCESS_TOKEN", "").strip()
    if not user_token:
        raise RuntimeError("FB_ACCESS_TOKEN is not configured")
    video_path = Path(os.environ.get("VIDEO_PATH", f"seed-{seed:02d}.mp4"))
    if not video_path.exists() or video_path.stat().st_size < 1_000_000:
        raise RuntimeError(f"Missing or invalid seed video: {video_path}")
    state_path = Path(f".social-state/casual-misconduct-seed-{seed:02d}.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "seed": seed,
        "page_id": PAGE_ID,
        "page_name": PAGE_NAME,
        "facebook": None,
        "instagram": None,
    }
    page = resolve_page(user_token)
    caption = CAPTIONS[seed]
    errors = {}
    if "facebook" in destinations and not state.get("facebook"):
        try:
            state["facebook"] = publish_facebook_reel(page, video_path, caption)
        except Exception as exc:
            errors["facebook"] = str(exc)
    if "instagram" in destinations and not state.get("instagram"):
        try:
            facebook_video_id = (state.get("facebook") or {}).get("video_id")
            if not facebook_video_id:
                raise RuntimeError("Facebook Reel must publish before Instagram can use the Meta-hosted source")
            video_url = None
            for _ in range(60):
                source_data = graph(
                    "GET",
                    facebook_video_id,
                    page["access_token"],
                    params={"fields": "source", "access_token": page["access_token"]},
                )
                video_url = source_data.get("source")
                if video_url:
                    break
                time.sleep(5)
            if not video_url:
                raise RuntimeError(f"Meta did not return a source URL for Facebook Reel {facebook_video_id} after 5 minutes")
            state["instagram"] = publish_instagram_reel(page, video_url, caption)
        except Exception as exc:
            errors["instagram"] = str(exc)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["errors"] = errors
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2))
    if errors:
        raise RuntimeError(json.dumps(errors))

if __name__ == "__main__":
    main()
