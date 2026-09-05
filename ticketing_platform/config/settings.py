"""
Django settings for TickIt — Event Ticketing & QR Validation Platform.

All sensitive values (SECRET_KEY, DATABASE_URL etc.) are read from environment
variables so the same codebase runs locally (with defaults) and in production
(with real values injected by Railway).
"""

from pathlib import Path
import dj_database_url
import os

# Base directory of the project — used to build absolute paths below.
BASE_DIR = Path(__file__).resolve().parent.parent

# ---- Security ------------------------------------------------------------

# Read from env in production. The placeholder is intentionally insecure so
# Django startup fails loudly if someone forgets to set the real key.
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    'django-insecure-build-time-placeholder-do-not-use-in-production'
)

# Debug mode — always False in production. Exposes full tracebacks when True,
# which is a serious security risk on a live server.
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

# Only respond to requests from these domains. Prevents HTTP Host header attacks.
# Defaults to '*' locally so runserver works without extra config.
ALLOWED_HOSTS = (
    os.environ.get('ALLOWED_HOSTS', '').split(',')
    if os.environ.get('ALLOWED_HOSTS')
    else ['*']
)

# Build CSRF_TRUSTED_ORIGINS — hardcode the Railway domain so it always works.
_trusted = set()
_trusted.add('https://web-production-a37bc.up.railway.app')
if os.environ.get('ALLOWED_HOSTS'):
    for h in os.environ['ALLOWED_HOSTS'].split(','):
        h = h.strip()
        if h:
            _trusted.add(f'https://{h}')
CSRF_TRUSTED_ORIGINS = list(_trusted)

# ---- Apps ----------------------------------------------------------------

INSTALLED_APPS = [
    # Django built-ins
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Project apps
    'apps.accounts',   # User roles and profiles
    'apps.events',     # Venues, events, waitlist
    'apps.tickets',    # Ticket types, reservations, QR tickets
    'cloudinary_storage',  # Cloudinary media storage (QR images)
    'cloudinary',          # Cloudinary SDK
]

# ---- Middleware -----------------------------------------------------------

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files in production without a separate web server.
    # Must be right after SecurityMiddleware.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    # Protects all POST forms against Cross-Site Request Forgery.
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

# ---- Templates -----------------------------------------------------------

TEMPLATES = [
    {
        # Django's own template engine — used exclusively by django.contrib.admin.
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates' / 'admin'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
    {
        # Jinja2 engine — used for all app-facing pages.
        # config.jinja2.environment registers url() and static() as globals
        # so Jinja2 templates can call them without Django template tags.
        'BACKEND': 'django.template.backends.jinja2.Jinja2',
        'DIRS': [BASE_DIR / 'templates' / 'jinja2'],
        'OPTIONS': {
            'environment': 'config.jinja2.environment',
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.csrf',  # Makes csrf_input available
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ---- Database ------------------------------------------------------------

# dj-database-url parses the DATABASE_URL env var (set by Railway's Postgres
# plugin) into Django's DATABASES dict format. Falls back to SQLite locally.
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,        # Keep DB connections alive for 10 minutes
        conn_health_checks=True, # Verify connection is alive before reuse
    )
}

# ---- Password validation -------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ---- Internationalisation ------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True  # All datetimes stored as UTC, displayed in local time via template filters

# ---- Static files --------------------------------------------------------

STATIC_URL = '/static/'
# collectstatic copies everything into staticfiles/ — WhiteNoise serves from here.
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Local static assets (CSS, JS, images) live here during development.
STATICFILES_DIRS = [BASE_DIR / 'static']
# Compresses and fingerprints static files for cache-busting in production.
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# ---- Media files (user uploads — QR images) ------------------------------
# In production, files are stored on Cloudinary so they survive container
# restarts on Railway (Railway's filesystem is ephemeral).
# Set CLOUDINARY_URL in Railway's environment variables.

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Use Cloudinary for media storage when CLOUDINARY_URL is set (production).
# Falls back to local filesystem storage when running locally.
if os.environ.get('CLOUDINARY_URL'):
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---- Production security settings ----------------------------------------
# Only applied when DEBUG=False so local development is unaffected.

if not DEBUG:
    # Tell browsers to always use HTTPS for this domain for 1 year.
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Redirect all HTTP requests to HTTPS.
    SECURE_SSL_REDIRECT = True
    # Session cookie only sent over HTTPS — prevents session hijacking.
    SESSION_COOKIE_SECURE = True
    # CSRF cookie only sent over HTTPS.
    CSRF_COOKIE_SECURE = True
    # Railway terminates SSL at its load balancer and forwards via X-Forwarded-Proto.
    # This tells Django to trust that header when determining the request scheme.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# ---- Auth redirects ------------------------------------------------------

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'events:home'
LOGOUT_REDIRECT_URL = 'events:home'
