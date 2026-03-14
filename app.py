import streamlit as st
import folium
from streamlit_folium import st_folium
from folium import MacroElement, FeatureGroup
from jinja2 import Template
from exiftool import ExifToolHelper
import tempfile
import os
import random
from PIL import Image
import pillow_heif
import math
import datetime
from PIL import ImageOps

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
    st.session_state.current_image   = None
    st.session_state.delete_display  = False
    st.session_state.selected_date = None
    st.session_state.date_confirmed = False
    st.session_state.exif_date      = None
    st.session_state.require_date  = True
    st.session_state.total_rounds  = 5
    st.session_state.round_history = []  # list of dicts per round
    st.session_state.last_dist_m   = None

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
    media_dir = os.path.join(os.path.dirname(__file__), "media")
    valid_exts = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".mp4", ".mov"}
    all_media  = [
        f for f in os.listdir(media_dir)
        if os.path.splitext(f)[1].lower() in valid_exts
    ]
    if not all_media:
        return

    chosen     = random.choice(all_media)
    media_path = os.path.join(media_dir, chosen)
    suffix     = os.path.splitext(chosen)[1].lower()
    st.session_state.is_video = suffix in {".mp4", ".mov"}

    if suffix in [".heic", ".heif"]:
        img = Image.open(media_path)
        img = ImageOps.exif_transpose(img)  # fix rotation before saving
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            img.convert("RGB").save(tmp, format="JPEG")
            st.session_state.current_media  = tmp.name
        st.session_state.delete_display = True
    else:
        st.session_state.current_media  = media_path
        st.session_state.delete_display = False
    
    st.session_state.exif_date = extract_exif_date(media_path)

    # Extract GPS — handle both photo and video tag schemas
    try:
        with ExifToolHelper() as et:
            tags = et.get_tags(media_path, tags=[])[0]  # get all tags

        # iPhone .mov: "Keys:GPSCoordinates" = "+lat+lng+alt/"
        if "Keys:GPSCoordinates" in tags:
            raw = tags["Keys:GPSCoordinates"]  # e.g. "+37.3318-122.0312+10.000/"
            import re
            parts = re.findall(r"[+-]?\d+\.?\d*", raw)
            if len(parts) >= 2:
                st.session_state.exif_pin = {
                    "lat": float(parts[0]),
                    "lng": float(parts[1])
                }

        # Standard EXIF (photos + some mp4)
        elif "EXIF:GPSLatitude" in tags:
            lat     = tags.get("EXIF:GPSLatitude")
            lng     = tags.get("EXIF:GPSLongitude")
            lat_ref = tags.get("EXIF:GPSLatitudeRef")
            lon_ref = tags.get("EXIF:GPSLongitudeRef")
            if lat and lng:
                if lat_ref and lat_ref.upper() == "S":
                    lat = -abs(lat)
                if lon_ref and lon_ref.upper() == "W":
                    lng = -abs(lng)
                st.session_state.exif_pin = {"lat": lat, "lng": lng}

        # QuickTime GPS (some Android .mp4)
        elif "QuickTime:GPSCoordinates" in tags:
            raw = tags["QuickTime:GPSCoordinates"]
            import re
            parts = re.findall(r"[+-]?\d+\.?\d*", raw)
            if len(parts) >= 2:
                st.session_state.exif_pin = {
                    "lat": float(parts[0]),
                    "lng": float(parts[1])
                }
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
    st.title("📍 MomentMappr")
    st.markdown("### How to play")
    st.markdown(
        "A photo or video will be shown in the sidebar. "
        "Place a pin on the map where you think the media was taken, "
        "and optionally, guess the date it was taken, then hit **Confirm**. "
        "Your total score is the cumulative distance between your guesses "
        "and the actual locations, as well as the days off — **lower is better!**"
    )
    st.divider()

    st.markdown("### Game settings")

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

    st.divider()

    if st.button("🚀 Start Game", use_container_width=True):
        st.session_state.total_distance = 0.0
        st.session_state.rounds         = 0
        st.session_state.last_dist_m    = None
        st.session_state.initialized    = False
        st.session_state.require_date   = require_date
        st.session_state.total_rounds   = total_rounds
        st.session_state.game_state     = "playing"
        st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
# GAME SCREEN
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.game_state == "playing":
    col_title, col_score = st.columns([5, 2])

    with col_title:
        st.title("📍 MomentMappr")

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
                <div style="font-size: 0.7rem; color: #888; margin-top: 2px;">(meters) + (10 meters per day)</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if not st.session_state.initialized:
        st.session_state.exif_pin     = None
        st.session_state.manual_pin   = None
        st.session_state.confirmed    = False
        st.session_state.map_center   = [39.3299, -76.6205]
        st.session_state.map_zoom     = 16
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

                    round_entry = {
                        "media_path": st.session_state.current_media,
                        "is_video":   st.session_state.is_video,
                        "dist_m":     None,
                        "day_delta":  None,
                        "exif_date":  st.session_state.exif_date,
                    }

                    if st.session_state.exif_pin:
                        dist = haversine_m(st.session_state.manual_pin, st.session_state.exif_pin)
                        st.session_state.last_dist_m     = dist
                        st.session_state.total_distance += dist
                        round_entry["dist_m"] = dist

                    if st.session_state.require_date and selected_date and st.session_state.exif_date:
                        day_delta = abs((selected_date - st.session_state.exif_date).days)
                        st.session_state.total_distance += day_delta * 10
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
                    if st.session_state.delete_display and st.session_state.current_image:
                        try:
                            os.unlink(st.session_state.current_image)
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
                if entry["media_path"] and os.path.exists(entry["media_path"]):
                    if entry["is_video"]:
                        st.video(entry["media_path"])
                    else:
                        img = Image.open(entry["media_path"])
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
                round_total = (entry["dist_m"] or 0) + (entry["day_delta"] or 0) * 10
                st.metric("🧮 Round total", fmt_distance(round_total))