# social/models.py
import base64
import hashlib

from django.conf import settings
from django.db import models


class EncryptedTextField(models.TextField):
    """Store sensitive values encrypted at rest using Django's SECRET_KEY."""

    prefix = "fernet$"

    @staticmethod
    def _fernet():
        from cryptography.fernet import Fernet

        key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value

        if isinstance(value, str) and value.startswith(self.prefix):
            return value

        encrypted = self._fernet().encrypt(str(value).encode()).decode()
        return f"{self.prefix}{encrypted}"

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value

        if not isinstance(value, str) or not value.startswith(self.prefix):
            return value

        token = value[len(self.prefix):]
        try:
            return self._fernet().decrypt(token.encode()).decode()
        except Exception:
            return value

    def to_python(self, value):
        if value in (None, ""):
            return value

        if isinstance(value, str) and value.startswith(self.prefix):
            token = value[len(self.prefix):]
            try:
                return self._fernet().decrypt(token.encode()).decode()
            except Exception:
                return value

        return value

class TelegramChannel(models.Model):
    """Store Telegram channel configuration"""
    
    name = models.CharField(max_length=100, help_text="Display name for the channel")
    channel_username = models.CharField(max_length=50, unique=True, 
                                        help_text="@username of the channel")
    channel_chat_id = models.CharField(max_length=50, unique=True, 
                                       help_text="Numeric chat ID (negative for channels)")
    
    # Bot configuration
    bot_token = models.CharField(max_length=150, help_text="Bot token from @BotFather")
    bot_username = models.CharField(max_length=50, blank=True, 
                                    help_text="Bot's username")
    
    # Settings
    enabled = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False, 
                                     help_text="Set to True after successful test")
    
    # Platform-specific settings
    default_hashtags = models.CharField(max_length=200, blank=True, 
                                        help_text="Default hashtags to append to posts")
    parse_mode = models.CharField(max_length=20, default='HTML', 
                                 choices=[('HTML', 'HTML'), ('Markdown', 'Markdown')])
    
    # Analytics
    subscriber_count = models.IntegerField(default=0, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.name} (@{self.channel_username})"
    
    def get_chat_id(self):
        """Return the chat ID as a string"""
        return str(self.channel_chat_id)

class SocialPlatform(models.Model):
    """Generic social platform configuration"""
    
    PLATFORM_CHOICES = [
        ('telegram', 'Telegram'),
        ('buffer', 'Buffer'),
    ]
    
    name = models.CharField(max_length=20, choices=PLATFORM_CHOICES, unique=True)
    enabled = models.BooleanField(default=True)
    
    # Platform-specific configurations as JSON
    config = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.get_name_display()} ({'Enabled' if self.enabled else 'Disabled'})"


class BufferAccount(models.Model):
    """Store a user's Buffer personal API key and connection state."""

    CONNECTION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('connected', 'Connected'),
        ('expired', 'Expired'),
        ('revoked', 'Revoked'),
        ('error', 'Error'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='buffer_accounts'
    )
    name = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text="Display name for this account (shown in sidebar)"
    )
    api_key = EncryptedTextField()
    api_url = models.URLField(
        max_length=500,
        default='https://ubuntureport.onrender.com/api/news/top-sources-recent/',
        help_text="Upstream API URL to fetch news from for this account"
    )
    token_expires_at = models.DateTimeField(blank=True, null=True)
    connection_status = models.CharField(
        max_length=20,
        choices=CONNECTION_STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'connection_status']),
        ]

    def __str__(self):
        return self.name or f"Buffer account for {self.user}"
