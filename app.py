import streamlit as st
import folium
from streamlit_folium import st_folium
from folium import MacroElement, FeatureGroup
from jinja2 import Template
from exiftool import ExifToolHelper
import tempfile
import os
import random
from PIL import Image, ImageOps
import pillow_heif
import math
import datetime
import base64
import uuid
import requests
from supabase import create_client
import io
import streamlit.components.v1 as components

def scroll_to_top():
    components.html(
        """
        <script>
            setTimeout(function() {
                var doc = window.parent.document;
                // Scroll every element that could possibly be the container
                doc.body.scrollTop = 0;
                doc.documentElement.scrollTop = 0;
                var els = doc.querySelectorAll('*');
                for (var i = 0; i < els.length; i++) {
                    try { els[i].scrollTop = 0; } catch(e) {}
                }
            }, 100);
        </script>
        """,
        height=0,
    )

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def extract_gps(media_path):
    import re
    try:
        with ExifToolHelper() as et:
            tags = et.get_tags(media_path, tags=[])[0]
        if "Keys:GPSCoordinates" in tags:
            parts = re.findall(r"[+-]?\d+\.?\d*", tags["Keys:GPSCoordinates"])
            if len(parts) >= 2:
                return {"lat": float(parts[0]), "lng": float(parts[1])}
        elif "EXIF:GPSLatitude" in tags:
            lat, lng     = tags.get("EXIF:GPSLatitude"), tags.get("EXIF:GPSLongitude")
            lat_ref, lon_ref = tags.get("EXIF:GPSLatitudeRef"), tags.get("EXIF:GPSLongitudeRef")
            if lat and lng:
                if lat_ref and lat_ref.upper() == "S": lat = -abs(lat)
                if lon_ref and lon_ref.upper() == "W": lng = -abs(lng)
                return {"lat": lat, "lng": lng}
        elif "QuickTime:GPSCoordinates" in tags:
            parts = re.findall(r"[+-]?\d+\.?\d*", tags["QuickTime:GPSCoordinates"])
            if len(parts) >= 2:
                return {"lat": float(parts[0]), "lng": float(parts[1])}
    except Exception:
        pass
    return None

def create_game(game_id, files, total_rounds, require_date, start_lat=20.0, start_lng=0.0, start_zoom=2, title=""):
    media_metadata = []

    for f in files:
        ext      = os.path.splitext(f.name)[1].lower()
        filename = f"{uuid.uuid4()}{ext}"
        path     = f"{game_id}/{filename}"
        data     = f.read()

        # Upload raw bytes
        try:
            supabase.storage.from_("media").upload(
                path,
                data,
                {"content-type": f.type}
            )
        except Exception as e:
            st.error(f"Upload failed for {f.name}: {e}")
            continue

        # Extract EXIF from the bytes before they leave your machine
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        gps  = extract_gps(tmp_path)   # returns {"lat": ..., "lng": ...} or None
        date = extract_exif_date(tmp_path)  # returns datetime.date or None
        os.unlink(tmp_path)

        media_metadata.append({
            "path":     path,
            "lat":      gps["lat"]  if gps  else None,
            "lng":      gps["lng"]  if gps  else None,
            "taken_on": date.isoformat() if date else None,
        })

    # Save game settings + per-file metadata
    supabase.table("games").insert({
        "game_id":        game_id,
        "total_rounds":   total_rounds,
        "require_date":   require_date,
        "media_metadata": media_metadata,
        "start_lat":      start_lat,
        "start_lng":      start_lng,
        "start_zoom":     start_zoom,
        "title":          title,
    }).execute()

def get_game_settings(game_id):
    result = supabase.table("games").select(
        "total_rounds, require_date, media_metadata, start_lat, start_lng, start_zoom, title"
    ).eq("game_id", game_id).execute()
    if result.data:
        return result.data[0]
    return None

def upload_images_to_supabase(game_id, files):
    """Upload a list of Streamlit UploadedFile objects."""
    paths = []
    for f in files:
        ext      = os.path.splitext(f.name)[1].lower()
        filename = f"{uuid.uuid4()}{ext}"
        path     = f"{game_id}/{filename}"
        supabase.storage.from_("media").upload(
            path,
            f.read(),
            {"content-type": f.type}
        )
        paths.append(path)
    return paths

def get_game_image_urls(game_id):
    result = supabase.storage.from_("media").list(
        path=game_id,
        options={"limit": 100, "offset": 0}
    )
    
    valid_exts = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
    urls = []
    for f in result:
        name = f.get("name", "")
        if not name or name == ".emptyFolderPlaceholder":
            continue
        if os.path.splitext(name)[1].lower() not in valid_exts:
            continue
        url = supabase.storage.from_("media").get_public_url(f"{game_id}/{name}")
        urls.append(url)
    
    return urls

def download_to_temp(url):
    """Download a remote image to a local temp file for exiftool."""
    ext  = os.path.splitext(url.split("?")[0])[-1] or ".jpg"
    resp = requests.get(url)
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(resp.content)
        return tmp.name

def fix_orientation(img):
    """Apply EXIF orientation so the image displays upright."""
    return ImageOps.exif_transpose(img)

def extract_exif_date(media_path):
    """Try multiple tag names across photo and video formats."""
    date_tags = [
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "QuickTime:CreateDate",
        "QuickTime:ContentCreateDate",
        "Keys:CreationDate",
        "XMP:CreateDate",
    ]
    try:
        with ExifToolHelper() as et:
            tags = et.get_tags(media_path, tags=date_tags)[0]

        for tag in date_tags:
            val = tags.get(tag)
            if val:
                # Strip timezone suffix if present e.g. "2023:05:14 13:22:01+01:00"
                val = val[:19]
                # EXIF dates use colons as separators in the date part
                for fmt in ["%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"]:
                    try:
                        return datetime.datetime.strptime(val, fmt).date()
                    except ValueError:
                        continue
    except Exception as e:
        st.error(f"Date extraction failed: {e}")
    return None

def display_media(filepath, max_height=300):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in {".mp4", ".mov"}:
        st.video(filepath)
    else:
        st.markdown(
            f"""
            <img
                src="data:image/jpeg;base64,{base64.b64encode(open(filepath, "rb").read()).decode()}"
                style="max-height:{max_height}px; width:100%; object-fit:contain; border-radius:8px;"
            >
            """,
            unsafe_allow_html=True,
        )

def display_image(filepath, max_height=300):
    img = Image.open(filepath)
    img = ImageOps.exif_transpose(img)  # fix rotation

    st.markdown(
        f"""
        <style>
            [data-testid="stImage"] img {{
                max-height: {max_height}px;
                width: auto;
                object-fit: contain;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.image(img)  # pass the corrected PIL image directly, not the filepath

pillow_heif.register_heif_opener()

# ── Session state ─────────────────────────────────────────────────────────────
if "game_state" not in st.session_state:
    st.session_state.game_state      = "menu"
    st.session_state.total_distance  = 0.0
    st.session_state.rounds          = 0
    st.session_state.initialized     = False
    st.session_state.exif_pin        = None
    st.session_state.manual_pin      = None
    st.session_state.confirmed       = False
    st.session_state.map_center      = [39.3299, -76.6205]
    st.session_state.map_zoom        = 16
    st.session_state.current_media   = None
    st.session_state.is_video        = False
    st.session_state.delete_display  = False
    st.session_state.selected_date = None
    st.session_state.date_confirmed = False
    st.session_state.exif_date      = None
    st.session_state.require_date  = True
    st.session_state.total_rounds  = 5
    st.session_state.round_history = []  # list of dicts per round
    st.session_state.last_dist_m   = None
    st.session_state.game_title = ""
    st.session_state.used_media = set()

# At the top of your app, after session state init
params  = st.query_params
game_id = params.get("game", None)

if game_id and "remote_game_id" not in st.session_state:
    st.session_state.remote_game_id    = game_id
    st.session_state.remote_image_urls = get_game_image_urls(game_id)

    settings = get_game_settings(game_id)
    if settings:
        st.session_state.total_rounds    = settings["total_rounds"]
        st.session_state.require_date    = settings["require_date"]
        st.session_state.game_metadata   = settings.get("media_metadata") or []
        st.session_state.map_center      = [
            settings.get("start_lat", 39.3299),
            settings.get("start_lng", -76.6205),
        ]
        st.session_state.map_zoom        = settings.get("start_zoom", 4)
        st.session_state.game_title      = settings.get("title") or ""
        st.session_state.settings_locked = True
    else:
        st.session_state.settings_locked = False
        st.warning(f"No game found with ID: {game_id}")

def haversine_m(pin1, pin2):
    R = 6371000.0
    lat1, lon1 = math.radians(pin1["lat"]), math.radians(pin1["lng"])
    lat2, lon2 = math.radians(pin2["lat"]), math.radians(pin2["lng"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return R * 2 * math.asin(math.sqrt(a))

class SmoothWheelZoom(MacroElement):
    _template = Template("""
        {% macro script(this, kwargs) %}
        (function() {
            var map = {{ this._parent.get_name() }};
            var targetZoom = map.getZoom();
            var timer = null;
            var lastTime = 0;
            var throttleMs = 400;
            map.scrollWheelZoom.disable();
            map.getContainer().addEventListener('wheel', function(e) {
                e.preventDefault();
                var now = Date.now();
                if (now - lastTime < throttleMs) return;
                lastTime = now;
                var direction = e.deltaY < 0 ? 1 : -1;
                targetZoom = Math.min(
                    Math.max(targetZoom + (direction * 0.5), map.getMinZoom()),
                    map.getMaxZoom()
                );
                clearTimeout(timer);
                timer = setTimeout(function() {
                    map.setZoom(targetZoom, { animate: true });
                }, 0);
            }, { passive: false });
        })();
        {% endmacro %}
    """)

def load_random_media():
    if "used_media" not in st.session_state:
        st.session_state.used_media = set()

    # ── Remote (Supabase) branch ──────────────────────────────────────────────
    if "remote_game_id" in st.session_state and "game_metadata" in st.session_state:
        available = [
            m for m in st.session_state.game_metadata
            if m["path"] not in st.session_state.used_media
        ]
        if not available:
            st.warning("All media has been used.")
            return

        meta       = random.choice(available)
        st.session_state.used_media.add(meta["path"])

        url        = supabase.storage.from_("media").get_public_url(meta["path"])
        media_path = download_to_temp(url)
        suffix     = os.path.splitext(media_path)[1].lower()

        st.session_state.current_media  = media_path
        st.session_state.delete_display = True
        st.session_state.is_video       = suffix in {".mp4", ".mov"}
        st.session_state.exif_pin       = {"lat": meta["lat"], "lng": meta["lng"]} if meta.get("lat") else None
        st.session_state.exif_date      = datetime.date.fromisoformat(meta["taken_on"]) if meta.get("taken_on") else None

    # ── Local branch ──────────────────────────────────────────────────────────
    else:
        media_dir  = os.path.join(os.path.dirname(__file__), "media")
        valid_exts = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".mp4", ".mov"}
        if not os.path.exists(media_dir):
            st.warning("No media folder found. Use a shared game link or add files to the media/ folder.")
            return

        all_media = [
            f for f in os.listdir(media_dir)
            if os.path.splitext(f)[1].lower() in valid_exts
        ]

        available = [f for f in all_media if f not in st.session_state.used_media]

        if not available:
            st.warning("All media has been used.")
            return

        chosen     = random.choice(available)
        st.session_state.used_media.add(chosen)

        media_path = os.path.join(media_dir, chosen)
        suffix     = os.path.splitext(chosen)[1].lower()
        st.session_state.is_video = suffix in {".mp4", ".mov"}

        if suffix in [".heic", ".heif"]:
            img = Image.open(media_path)
            img = ImageOps.exif_transpose(img)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                img.convert("RGB").save(tmp, format="JPEG")
                st.session_state.current_media  = tmp.name
            st.session_state.delete_display = True
        else:
            st.session_state.current_media  = media_path
            st.session_state.delete_display = False

        st.session_state.exif_date = extract_exif_date(media_path)

        try:
            import re
            with ExifToolHelper() as et:
                tags = et.get_tags(media_path, tags=[])[0]
            if "Keys:GPSCoordinates" in tags:
                parts = re.findall(r"[+-]?\d+\.?\d*", tags["Keys:GPSCoordinates"])
                if len(parts) >= 2:
                    st.session_state.exif_pin = {"lat": float(parts[0]), "lng": float(parts[1])}
            elif "EXIF:GPSLatitude" in tags:
                lat, lng     = tags.get("EXIF:GPSLatitude"), tags.get("EXIF:GPSLongitude")
                lat_ref, lon_ref = tags.get("EXIF:GPSLatitudeRef"), tags.get("EXIF:GPSLongitudeRef")
                if lat and lng:
                    if lat_ref and lat_ref.upper() == "S": lat = -abs(lat)
                    if lon_ref and lon_ref.upper() == "W": lng = -abs(lng)
                    st.session_state.exif_pin = {"lat": lat, "lng": lng}
            elif "QuickTime:GPSCoordinates" in tags:
                parts = re.findall(r"[+-]?\d+\.?\d*", tags["QuickTime:GPSCoordinates"])
                if len(parts) >= 2:
                    st.session_state.exif_pin = {"lat": float(parts[0]), "lng": float(parts[1])}
        except Exception as e:
            st.error(f"EXIF extraction failed: {e}")

def fmt_distance(m):
    """Format a distance in metres, switching to km when >= 1000 m."""
    if m >= 1000:
        return f"{m / 1000:.2f} km"
    return f"{m:.0f} m"

st.markdown(
    """
    <style>
        /* Sidebar padding */
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {
            gap: 0.25rem;
        }

        /* Divider margins */
        [data-testid="stSidebar"] hr {
            margin-top: 0.2rem;
            margin-bottom: 0.2rem;
        }

        /* Metric label/value spacing */
        [data-testid="stSidebar"] [data-testid="stMetric"] {
            padding-top: 0.1rem;
            padding-bottom: 0.1rem;
        }

        /* Header spacing */
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {
            margin-top: 0rem;
            margin-bottom: 0rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1rem;
        }

        [data-testid="stSidebar"] > div:first-child {
            padding-top: 1rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(
    """
    <style>
        [data-testid="stSidebar"] > div:first-child {
            padding-top: 0.5rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)
# ═════════════════════════════════════════════════════════════════════════════
# MENU SCREEN
# ═════════════════════════════════════════════════════════════════════════════
if st.session_state.game_state == "menu":
    scroll_to_top()
    st.title("📍 MomentMappr")
    if st.session_state.get("game_title"):
        st.subheader(st.session_state.game_title)
    st.markdown("### How to play")
    st.markdown(
        "A photo or video will be shown in the sidebar. "
        "Place a pin on the map where you think the media was taken, "
        "and if required, guess the date it was taken, then hit **Confirm**. "
        "Your total score is the cumulative distance between your guesses "
        "and the actual locations, as well as the days off — **lower is better!**"
    )

    st.divider()
    st.markdown("### Game settings")

    if st.session_state.get("settings_locked"):
        # Show locked settings as read-only
        st.info(f"🔒 This is a shared game — settings are fixed by the creator.")
        st.markdown(f"**Rounds:** {st.session_state.total_rounds}")
        st.markdown(f"**Date guessing:** {'Required' if st.session_state.require_date else 'Off'}")
        total_rounds  = st.session_state.total_rounds
        require_date  = st.session_state.require_date
    else:
        total_rounds = st.number_input(
            "Number of rounds",
            min_value=1,
            max_value=50,
            value=st.session_state.total_rounds,
            step=1,
        )
        require_date = st.toggle(
            "Require date guess",
            value=st.session_state.require_date,
            help="If on, you must also guess the date the media was taken."
        )

    if not st.session_state.get("settings_locked"):
        st.markdown("Play now with random photos and videos around **Johns Hopkins University Homewood Campus**")

    if st.button("🚀 Start Game", use_container_width=True):
        if "game_metadata" in st.session_state and st.session_state.get("settings_locked"):
            max_rounds = len(st.session_state.game_metadata)
        else:
            media_dir  = os.path.join(os.path.dirname(__file__), "media")
            valid_exts = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".mp4", ".mov"}
            if os.path.exists(media_dir):
                max_rounds = len([
                    f for f in os.listdir(media_dir)
                    if os.path.splitext(f)[1].lower() in valid_exts
                ])
            else:
                max_rounds = 0

        capped_rounds = min(total_rounds, max_rounds)
        if capped_rounds < total_rounds:
            st.warning(f"Only {max_rounds} media files available — rounds capped to {capped_rounds}.")

        st.session_state.total_distance = 0.0
        st.session_state.rounds         = 0
        st.session_state.last_dist_m    = None
        st.session_state.initialized    = False
        st.session_state.require_date   = require_date
        st.session_state.total_rounds   = total_rounds
        st.session_state.round_history  = []
        st.session_state.used_media = set()
        st.session_state.game_state     = "playing"
        st.rerun()

    st.divider()
    st.markdown("### Custom game maker")
    st.markdown("Or you can create a custom game with your own photos to send to friends!")
    if st.button("📤 Create Custom Game", use_container_width=True):
        st.session_state.game_state = "upload"
        st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
# GAME SCREEN
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.game_state == "playing":
    scroll_to_top()
    col_title, col_score = st.columns([5, 2])

    with col_title:
        st.title("📍 MomentMappr")
        if st.session_state.get("game_title"):
            st.subheader(st.session_state.game_title)

    with col_score:
        st.markdown(
            f"""
            <div style="
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 10px;
                padding: 10px 14px;
                text-align: center;
                margin-top: 8px;
            ">
                <div style="font-size: 0.75rem; color: #aaa; margin-bottom: 2px;">TOTAL SCORE</div>
                <div style="font-size: 1.1rem; font-weight: 700; color: #fff;">{fmt_distance(st.session_state.total_distance)}</div>
                <div style="font-size: 0.7rem; color: #888; margin-top: 2px;">(meters) + (1 meter per day)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if not st.session_state.initialized:
        st.session_state.exif_pin     = None
        st.session_state.manual_pin   = None
        st.session_state.confirmed    = False
        # Only reset map position on the very first round of the game,
        # not on every round — and don't overwrite shared link start position
        if st.session_state.rounds == 0:
            if "remote_game_id" not in st.session_state:
                # local game — use JHU default
                st.session_state.map_center = [39.3299, -76.6205]
                st.session_state.map_zoom   = 16
            # else: shared link already set map_center and map_zoom from settings, leave them
        load_random_media()
        st.session_state.initialized  = True
        st.session_state.selected_date = None
        st.session_state.last_dist_m   = None

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        if st.button("🏠 Back to Menu", use_container_width=True):
            if st.session_state.delete_display and st.session_state.current_media:
                try:
                    os.unlink(st.session_state.current_media)
                except Exception:
                    pass
            st.session_state.game_state  = "menu"
            st.session_state.initialized = False
            st.rerun()

        st.divider()

        current_round = st.session_state.rounds if st.session_state.confirmed else st.session_state.rounds + 1
        st.header(f"Round {current_round} / {st.session_state.total_rounds}")
        st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
        if st.session_state.current_media:
            if st.session_state.is_video:
                display_media(st.session_state.current_media, max_height=300)
            else:
                display_image(st.session_state.current_media, max_height=300)
        else:
            st.warning("No media found in the media/ folder.")

        if not st.session_state.confirmed:
            selected_date = st.session_state.selected_date
            if st.session_state.require_date:
                st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)

                selected_date = st.date_input(
                    "📅 Date taken",
                    value=None,
                    min_value=datetime.date(2000, 1, 1),
                    max_value=datetime.date.today(),
                )
                # Only store it if the user actually picked something (not None)
                st.session_state.selected_date = selected_date if selected_date else None

                if st.session_state.require_date and not selected_date:
                    st.caption("⚠️ Please select a date to confirm.")

            # Gate confirm on both pin AND date (if required)
            pin_ready  = st.session_state.manual_pin is not None
            date_ready = (not st.session_state.require_date) or (selected_date is not None)

            if pin_ready and date_ready:
                st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
                if st.button("✅ Confirm", use_container_width=True):
                    st.session_state.confirmed = True
                    st.session_state.rounds   += 1

                    # In the confirm button handler, when building round_entry:
                    round_entry = {
                        "media_path": st.session_state.current_media,
                        "is_video":   st.session_state.is_video,
                        "dist_m":     None,
                        "day_delta":  None,
                        "exif_date":  st.session_state.exif_date,
                        "media_bytes": None,
                    }

                    # Read and store bytes immediately at confirm time
                    if st.session_state.current_media and os.path.exists(st.session_state.current_media):
                        with open(st.session_state.current_media, "rb") as f:
                            round_entry["media_bytes"] = f.read()

                    if st.session_state.exif_pin:
                        dist = haversine_m(st.session_state.manual_pin, st.session_state.exif_pin)
                        st.session_state.last_dist_m     = dist
                        st.session_state.total_distance += dist
                        round_entry["dist_m"] = dist

                    if st.session_state.require_date and selected_date and st.session_state.exif_date:
                        day_delta = abs((selected_date - st.session_state.exif_date).days)
                        st.session_state.total_distance += day_delta * 1
                        round_entry["day_delta"] = day_delta

                    st.session_state.round_history.append(round_entry)
                    st.rerun()
        else:
            if st.session_state.require_date:
                st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
                st.date_input(
                    "📅 Date taken",
                    value=st.session_state.selected_date,
                    disabled=True,
                )
            if st.session_state.rounds < st.session_state.total_rounds:
                st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
                if st.button("🔀 Next Round", use_container_width=True):
                    if st.session_state.delete_display and st.session_state.current_media:
                        try:
                            os.unlink(st.session_state.current_media)
                        except Exception:
                            pass
                    st.session_state.initialized = False
                    st.session_state.last_dist_m = None
                    st.rerun()

        if st.session_state.confirmed:
            if st.session_state.rounds >= st.session_state.total_rounds:
                st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
                if st.button("🏁 See Final Score", use_container_width=True):
                    st.session_state.game_state = "gameover"
                    st.rerun()

            st.divider()
            st.markdown("<div style='margin-top: 2rem;'></div>", unsafe_allow_html=True)
            if st.session_state.last_dist_m is not None:
                st.metric("📏 This round", fmt_distance(st.session_state.last_dist_m))

            if st.session_state.selected_date and st.session_state.exif_date:
                day_delta = abs((st.session_state.selected_date - st.session_state.exif_date).days)
                st.metric("📅 Date off by", f"{day_delta} day{'s' if day_delta != 1 else ''}")
                st.caption(f"Actual date: {st.session_state.exif_date.strftime('%b %d, %Y')}")
            elif not st.session_state.exif_date:
                st.caption("No date metadata found in this media.")

            if not st.session_state.exif_pin:
                st.warning("No GPS data found in this media.")

    # ── Map ───────────────────────────────────────────────────────────────────
    m = folium.Map(
        location=st.session_state.map_center,
        zoom_start=st.session_state.map_zoom,
        tiles="OpenStreetMap",
    )
    SmoothWheelZoom().add_to(m)

    fg = FeatureGroup(name="pins")

    if st.session_state.confirmed and st.session_state.exif_pin:
        folium.Marker(
            location=[st.session_state.exif_pin["lat"], st.session_state.exif_pin["lng"]],
            icon=folium.Icon(color="blue", icon="camera"),
        ).add_to(fg)

    if st.session_state.manual_pin:
        folium.Marker(
            location=[st.session_state.manual_pin["lat"], st.session_state.manual_pin["lng"]],
            icon=folium.Icon(color="red", icon="map-marker"),
        ).add_to(fg)

    if st.session_state.confirmed and st.session_state.manual_pin and st.session_state.exif_pin:
        folium.PolyLine(
            locations=[
                [st.session_state.manual_pin["lat"], st.session_state.manual_pin["lng"]],
                [st.session_state.exif_pin["lat"],   st.session_state.exif_pin["lng"]],
            ],
            color="gray", weight=2, dash_array="6",
        ).add_to(fg)

    map_data = st_folium(
        m,
        width="100%",
        height=560,
        feature_group_to_add=fg,
        returned_objects=["last_clicked", "zoom", "center"],
    )

    if not st.session_state.confirmed and map_data and map_data.get("last_clicked"):
        click   = map_data["last_clicked"]
        new_pin = {"lat": click["lat"], "lng": click["lng"]}
        if new_pin != st.session_state.manual_pin:
            st.session_state.manual_pin = new_pin
            if map_data.get("zoom"):
                st.session_state.map_zoom = map_data["zoom"]
            if map_data.get("center"):
                st.session_state.map_center = [
                    map_data["center"]["lat"],
                    map_data["center"]["lng"],
                ]
            st.rerun()
# ═════════════════════════════════════════════════════════════════════════════
# GAME OVER SCREEN
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.game_state == "gameover":
    scroll_to_top()
    st.title("🏁 Game Over")

    # Final score box
    st.markdown(
        f"""
        <div style="
            background-color: #1e1e1e;
            border: 1px solid #444;
            border-radius: 14px;
            padding: 20px 28px;
            text-align: center;
            margin-bottom: 1.5rem;
        ">
            <div style="font-size: 1rem; color: #aaa; margin-bottom: 6px;">FINAL SCORE</div>
            <div style="font-size: 2.2rem; font-weight: 800; color: #fff;">
                {fmt_distance(st.session_state.total_distance)}
            </div>
            <div style="font-size: 0.8rem; color: #888; margin-top: 6px;">
                {st.session_state.total_rounds} rounds
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.button("🔁 Play Again", use_container_width=True):
        st.session_state.game_state    = "menu"
        st.session_state.round_history = []
        st.session_state.initialized   = False
        st.rerun()

    st.divider()
    st.subheader("📋 Round Summary")

    for i, entry in enumerate(st.session_state.round_history):
        with st.expander(f"Round {i + 1}", expanded=True):
            col_media, col_stats = st.columns([1, 1])

            with col_media:
                if entry.get("media_bytes"):
                    if entry["is_video"]:
                        st.video(entry["media_bytes"])
                    else:
                        img = Image.open(io.BytesIO(entry["media_bytes"]))
                        img = ImageOps.exif_transpose(img)
                        st.image(img, use_container_width=True)
                else:
                    st.caption("Media no longer available.")

            with col_stats:
                if entry["dist_m"] is not None:
                    st.metric("📍 Distance off", fmt_distance(entry["dist_m"]))
                else:
                    st.caption("No GPS data for this round.")

                if entry["day_delta"] is not None:
                    st.metric(
                        "📅 Date off by",
                        f"{entry['day_delta']} day{'s' if entry['day_delta'] != 1 else ''}"
                    )
                    if entry["exif_date"]:
                        st.caption(f"Actual date: {entry['exif_date'].strftime('%b %d, %Y')}")
                elif st.session_state.require_date:
                    st.caption("No date metadata for this round.")

                # Round subtotal
                round_total = (entry["dist_m"] or 0) + (entry["day_delta"] or 0) * 1
                st.metric("🧮 Round total", fmt_distance(round_total))
# ═════════════════════════════════════════════════════════════════════════════
# CREATE CUSTOM SCREEN
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.game_state == "upload":
    scroll_to_top()
    st.title("📤 Create a Custom Game")

    game_title = st.text_input(
        "Game title",
        placeholder="e.g. College Memories, World Trip, Family Album, ...",
        max_chars=60,
    )

    uploaded_files = st.file_uploader(
        "Upload images only (max 8MB per file)",
        type=["jpg", "jpeg", "heic", "png"],
        accept_multiple_files=True,
    )

    # filter oversized files
    MAX_FILE_BYTES = 8 * 1024 * 1024
    if uploaded_files:
        preview_pins = []
        for f in uploaded_files:
            ext = os.path.splitext(f.name)[1].lower()
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(f.read())
                tmp_path = tmp.name
            f.seek(0)  # reset file pointer so it can be read again later during upload
            gps = extract_gps(tmp_path)
            os.unlink(tmp_path)
            if gps:
                preview_pins.append({"lat": gps["lat"], "lng": gps["lng"], "name": f.name})
        st.session_state.upload_preview_pins = preview_pins
    else:
        st.session_state.upload_preview_pins = []

    num_files = len(uploaded_files) if uploaded_files else 0

    st.divider()
    st.markdown("### Starting map location")
    st.caption("Pan and zoom to where you want players to start.")

    # init upload map state only once
    if "upload_map_center" not in st.session_state:
        st.session_state.upload_map_center = [20.0, 0.0]
    if "upload_map_zoom" not in st.session_state:
        st.session_state.upload_map_zoom = 2

    upload_map = folium.Map(
        location=st.session_state.upload_map_center,
        zoom_start=st.session_state.upload_map_zoom,
        tiles="OpenStreetMap",
    )
    SmoothWheelZoom().add_to(upload_map)

    # After SmoothWheelZoom().add_to(upload_map), add:
    for pin in st.session_state.get("upload_preview_pins", []):
        folium.Marker(
            location=[pin["lat"], pin["lng"]],
            icon=folium.Icon(color="blue", icon="camera"),
            tooltip=pin["name"],
        ).add_to(upload_map)

    upload_map_data = st_folium(
        upload_map,
        width="100%",
        height=400,
        returned_objects=["center", "zoom"],
        key="upload_map",
    )

    st.divider()
    st.markdown("### Game settings")

    upload_total_rounds = st.number_input(
        "Number of rounds",
        min_value=1,
        max_value=max(num_files, 1),
        value=min(5, max(num_files, 1)),
        step=1,
        disabled=num_files == 0,
        help="Cannot exceed the number of uploaded files.",
    )

    upload_require_date = st.toggle(
        "Require date guess",
        value=True,
        help="If on, players must also guess the date the media was taken."
    )

    st.divider()

    can_create = num_files > 0 and upload_total_rounds <= num_files

    if num_files == 0:
        st.info("Upload at least one file to create a game.")

    if can_create:
        st.write(f"{num_files} file(s) selected")
        if st.button("🚀 Create Shareable Game", use_container_width=True):
            if upload_map_data and upload_map_data.get("center"):
                start_lat  = upload_map_data["center"]["lat"]
                start_lng  = upload_map_data["center"]["lng"]
                start_zoom = upload_map_data.get("zoom") or 2
            else:
                start_lat  = st.session_state.upload_map_center[0]
                start_lng  = st.session_state.upload_map_center[1]
                start_zoom = st.session_state.upload_map_zoom

            game_id = str(uuid.uuid4())[:8]
            with st.spinner("Uploading..."):
                create_game(
                    game_id,
                    uploaded_files,
                    upload_total_rounds,
                    upload_require_date,
                    start_lat,
                    start_lng,
                    start_zoom,
                    title=game_title,
                )
            share_url = f"https://momentmappr.streamlit.app/?game={game_id}"
            st.success("Game created!")
            st.code(share_url)
            st.caption("Share this link with anyone to play with your images.")

    if st.button("🏠 Back to Menu", use_container_width=True):
        st.session_state.game_state = "menu"
        st.rerun()