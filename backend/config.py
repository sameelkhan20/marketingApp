import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_URL = f"sqlite:///{DATA_DIR / 'app.db'}"
FRONTEND_DIR = ROOT / "frontend"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
# Text AI (plans, learning, alert advice): Claude when ANTHROPIC_API_KEY is set, else Gemini.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
# Plans are the money decision: a stronger model writes and reviews them (a few cents a plan).
PLAN_MODEL = os.getenv("PLAN_MODEL", "gemini-pro-latest")
_raw_image_key = os.getenv("IMAGE_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") or (_raw_image_key if _raw_image_key and not _raw_image_key.startswith("sk-") else "")  # Google keys: AIza... or AQ....
PKR_PER_USD = float(os.getenv("PKR_PER_USD", "280"))  # fallback only: the live rate is used (backend/fx.py)
FX_LIVE = os.getenv("FX_LIVE", "1").lower() not in ("0", "false", "no")

META_APP_ID = os.getenv("META_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
META_API_VERSION = os.getenv("META_API_VERSION", "v21.0")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")


MONITOR_INTERVAL_HOURS = float(os.getenv("MONITOR_INTERVAL_HOURS", "3"))

# AI creatives (images), same setup as the Social Agent:
# IMAGE_PROVIDER = higgsfield (default: the team's plan credits, connected on the Connections page)
#                | gemini | openai (these need IMAGE_API_KEY).
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "higgsfield").lower()
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY", "") or (os.getenv("GEMINI_API_KEY", "") if IMAGE_PROVIDER == "gemini"
                                                   else os.getenv("OPENAI_API_KEY", ""))
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "")
HIGGSFIELD_MCP_URL = os.getenv("HIGGSFIELD_MCP_URL", "https://mcp.higgsfield.ai/mcp")
HIGGSFIELD_MODEL = os.getenv("HIGGSFIELD_MODEL", "gpt_image_2_5")
HIGGSFIELD_VARIANT = os.getenv("HIGGSFIELD_VARIANT", "sunburst")
HIGGSFIELD_QUALITY = os.getenv("HIGGSFIELD_QUALITY", "medium")
HIGGSFIELD_RESOLUTION = os.getenv("HIGGSFIELD_RESOLUTION", "1k")
HIGGSFIELD_ASPECT_RATIO = os.getenv("HIGGSFIELD_ASPECT_RATIO", "4:5")
HIGGSFIELD_BACKGROUND = os.getenv("HIGGSFIELD_BACKGROUND", "auto")
# Cloudflare R2 (public bucket): Higgsfield imports the product photo by URL. Same as the Social Agent.
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "")
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

# Email alerts, same setup as the Social Agent: the Gmail API is tried first (lands in the inbox),
# Brevo's API when Gmail is not set up or refuses. ALERT_EMAIL may hold several addresses.
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "") or os.getenv("ALERT_EMAIL_TO", "")
ALERT_MIN_SEVERITY = os.getenv("ALERT_MIN_SEVERITY", "warning")  # info | warning | critical
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
ALERT_FROM = os.getenv("ALERT_FROM", "")
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "0"))  # 0 = Monday ... 6 = Sunday
