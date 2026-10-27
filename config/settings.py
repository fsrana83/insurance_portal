"""Django settings for the Motor Vehicle Policy Issuance System."""
import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-change-me-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",  # lets custom widget templates in /templates be found
    "core",
    "motor",
    "reinsurance",
    "claims",
    "payments",
    "accounting",
    "budgeting",
    "pnc",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.audit.CurrentUserMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.app_context",
            ],
        },
    },
]
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"
WSGI_APPLICATION = "config.wsgi.application"

# ---- Database: SQLite for development, PostgreSQL for production -----------
if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", "postgres"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
            # WAL (enabled by core.apps via a connection signal) lets the demo-data loader
            # write while pages are read; works on every Django version
            "OPTIONS": {"timeout": 30},
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = "Asia/Muscat"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Auto-logout after 30 minutes of inactivity (FRD 2.2)
SESSION_COOKIE_AGE = 30 * 60
SESSION_SAVE_EVERY_REQUEST = True

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "motor@insurer.example")

# Celery (reminders). Optional: the management command works without it.
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# ---------------------------------------------------------------------------
# Business configuration
# ---------------------------------------------------------------------------
APP_NAME = "Advance Motor Insurance Solution"
BASE_CURRENCY = "OMR"

# Oman VAT is configurable; confirm the treatment of motor premiums with your tax team.
VAT_RATE = Decimal(os.environ.get("MOTOR_VAT_RATE", "0.05"))

# Driver younger than this triggers the imposed / young-driver excess.
YOUNG_DRIVER_AGE = 25

# FSA unified motor policy amendments (Administrative Decision 1/2026):
# repairs must finish within 30 days of the claim file being finalised and
# repair payments are released 70% before repair / 30% after completion.
REPAIR_SLA_DAYS = 30
REPAIR_INTERIM_PCT = Decimal("70")
# Daily delay compensation: the FSA will issue the rate separately -> configure here (base currency).
REPAIR_DELAY_DAILY_COMPENSATION = Decimal(os.environ.get("MOTOR_REPAIR_DELAY_DAILY", "0.000"))

# Surveyor turnaround SLA in days (addendum 4.4)
SURVEYOR_SLA_DAYS = 3
# Renewal reminder offsets (days before expiry)
RENEWAL_REMINDER_DAYS = (30, 7, 0)

# Claims above this amount (base currency) need the Claims Manager / Head of Claims (FRD 6.2)
CLAIM_MANAGER_LIMIT = Decimal("5000")

# --- Payments / AI / accounting -------------------------------------------------------------
PAYMENT_GATEWAY = os.environ.get("PAYMENT_GATEWAY", "demo")          # see payments/gateways.py
PAYMENT_LINK_VALIDITY_DAYS = 7
SHARE_LINK_VALIDITY_DAYS = 14                                         # secure document links (WhatsApp / e-mail)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")  # local Ollama installation
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "180"))
# IFRS 17 PAA: motor cover is <= 1 year, so acquisition cash flows may be expensed as incurred (IFRS 17.59(a)).
IFRS17_EXPENSE_IACF = os.environ.get("IFRS17_EXPENSE_IACF", "1") == "1"
DEFAULT_DEPRECIATION_PCT = Decimal("15")                               # reducing balance, % per annum


# --- sixth release -----------------------------------------------------------------------------------------------
# Maker-checker: changes to policies and claims wait for a second person's confirmation.
DUAL_CONFIRMATION = True
REQUIRE_PAYMENT_OR_CREDIT = True      # a policy is issued against a receipt, or on credit approved by Credit Control
# Indicative cost per injured person when no figure has been entered (base currency). Replace with your own tariff / diyya schedule.
INJURY_COST_TABLE = {"MINOR": Decimal("500"), "MODERATE": Decimal("2500"), "SEVERE": Decimal("12000"), "DISABILITY": Decimal("25000"), "DEATH": Decimal("10000")}
LEGAL_COST_ALLOWANCE = Decimal("750")

# ---- Property & Casualty
PNC_AGENT_AUTHORITY = Decimal("100000")      # total sum insured (base currency) an agent / broker may bind without an underwriter
PNC_POLICY_FEE = Decimal("5")                 # per policy, base currency
PNC_LOADING_LIMIT = Decimal("25")             # loading / discount % beyond which an underwriter must approve

# Claim head limits (RO). The requirements document marks several of these "to be confirmed", so they are settings, not code.
CLAIM_HEAD_LIMITS = {"PA": Decimal(os.environ.get("MOTOR_LIMIT_PA", "15000")),
                     "TPBI": Decimal(os.environ.get("MOTOR_LIMIT_TPBI", "100000")),
                     "MEDICAL": Decimal(os.environ.get("MOTOR_LIMIT_MEDICAL", "0")),      # 0 = no limit until confirmed
                     "AMBULANCE": Decimal(os.environ.get("MOTOR_LIMIT_AMBULANCE", "0")),
                     "TOWAGE": Decimal(os.environ.get("MOTOR_LIMIT_TOWAGE", "50"))}
CASH_SETTLEMENT_PCT = Decimal(os.environ.get("MOTOR_CASH_PCT", "80"))   # cash in lieu of repair: max % of the lowest garage quotation

# Largest P&C reserve movement (base currency) each role can make without a claims manager's approval
PNC_RESERVE_AUTHORITY = {"CLAIMS_HANDLER": Decimal(os.environ.get("MOTOR_PNC_RESERVE_AUTH", "20000"))}
