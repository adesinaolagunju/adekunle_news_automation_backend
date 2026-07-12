# core/settings.py
import os
from pathlib import Path
from dotenv import load_dotenv
import dj_database_url
from datetime import timedelta

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-your-secret-key-here')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = ['*']

STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third party
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
    
    # Local
    'news',
    'social',
    'posts',
    'accounts',
    'api',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=7),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

WSGI_APPLICATION = 'core.wsgi.application'

# Database
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        # default="postgresql://adekunle:gOEgNARWMelB51tgX6D3t71TJUhmVkq0@dpg-d9304l6rnols7381mh00-a.oregon-postgres.render.com/adekunle",
        default="postgresql://postgres.fvofpqzlpllsoshdjpkw:sulaimanadekunle@aws-0-eu-west-3.pooler.supabase.com:5432/postgres",
        conn_max_age=0,
        ssl_require=True,
    )
}

# CORS
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

# Static & Media
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default image fallback for news without a usable image
SITE_DOMAIN = os.getenv('SITE_DOMAIN', 'localhost:8000')
_scheme = 'http' if DEBUG else 'https'
DEFAULT_NEWS_IMAGE_URL = os.getenv(
    'DEFAULT_NEWS_IMAGE_URL',
    f'{_scheme}://{SITE_DOMAIN}/static/images/default_new_image.png',
)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "monitor": {
            "format": (
                "[%(asctime)s] %(levelname)s %(name)s "
                "%(message)s"
            ),
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "monitor",
        },
    },
    "loggers": {
        "db.monitoring": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# Activate DB connection / job monitoring (patches DB backend at import).
import core.monitoring  # noqa: E402, F401




# Swagger/OpenAPI settings
SPECTACULAR_SETTINGS = {
    'TITLE': 'Adekunle News Automation API',
    'DESCRIPTION': '''
    ## Adekunle News Automation Platform
    
    This API powers the Adekunle News Automation system, allowing you to:
    
    ### Core Features
    - **News Management**: Fetch, filter, and manage news articles
    - **Social Media Integration**: Post to Telegram and Buffer
    - **Automation**: Schedule and queue posts automatically
    - **Analytics**: Track posting history and performance
    
    ### Authentication
    All endpoints require authentication. Use the `/api/auth/login/` endpoint to obtain a session cookie or use Token authentication.
    
    ### Rate Limiting
    Rate limits apply to protect the system. Contact the admin for custom limits.
    ''',
    'VERSION': '1.0.0',
    'CONTACT': {
        'name': 'Adekunle News Team',
        'email': 'support@adekunlereport.com',
        'url': 'https://adekunlereport.com',
    },
    'LICENSE': {
        'name': 'MIT License',
        'url': 'https://opensource.org/licenses/MIT',
    },
    'SERVE_INCLUDE_SCHEMA': True,
    'SWAGGER_UI_SETTINGS': {
        'deepLinking': True,
        'persistAuthorization': True,
        'displayOperationId': True,
        'filter': True,  # Enable search/filter in docs
        'showExtensions': True,
        'showCommonExtensions': True,
        'tryItOutEnabled': True,
    },
    'TAGS': [
        {'name': 'auth', 'description': 'Authentication endpoints'},
        {'name': 'news', 'description': 'News management endpoints'},
        {'name': 'categories', 'description': 'Category management'},
        {'name': 'countries', 'description': 'Country management'},
        {'name': 'filter-rules', 'description': 'News filter rules'},
        {'name': 'telegram-channels', 'description': 'Telegram channel configuration'},
        {'name': 'platforms', 'description': 'Social platform management'},
        {'name': 'posts', 'description': 'Post jobs and history'},
        {'name': 'stats', 'description': 'Dashboard statistics'},
        {'name': 'settings', 'description': 'System settings'},
    ],
    'EXTENSIONS': {
        'x-version': '1.0.0',
        'x-organization': 'Adekunle News',
    },
}
