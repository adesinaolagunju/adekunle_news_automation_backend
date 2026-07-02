# social/models.py
from django.db import models

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
        ('twitter', 'X (Twitter)'),
        ('facebook', 'Facebook'),
        ('instagram', 'Instagram'),
        ('telegram', 'Telegram'),
    ]
    
    name = models.CharField(max_length=20, choices=PLATFORM_CHOICES, unique=True)
    enabled = models.BooleanField(default=True)
    
    # Platform-specific configurations as JSON
    config = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.get_name_display()} ({'Enabled' if self.enabled else 'Disabled'})"