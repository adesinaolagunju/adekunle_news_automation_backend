# social/admin.py
from django.contrib import admin
from .models import BufferAccount, TelegramChannel, SocialPlatform

@admin.register(TelegramChannel)
class TelegramChannelAdmin(admin.ModelAdmin):
    list_display = ['name', 'channel_username', 'enabled', 'is_verified', 'subscriber_count']
    list_editable = ['enabled']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Channel Info', {
            'fields': ('name', 'channel_username', 'channel_chat_id')
        }),
        ('Bot Configuration', {
            'fields': ('bot_token', 'bot_username', 'is_verified')
        }),
        ('Settings', {
            'fields': ('enabled', 'default_hashtags', 'parse_mode', 'subscriber_count')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

@admin.register(SocialPlatform)
class SocialPlatformAdmin(admin.ModelAdmin):
    list_display = ['get_name_display', 'enabled']
    list_editable = ['enabled']


@admin.register(BufferAccount)
class BufferAccountAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'connection_status', 'token_expires_at', 'created_at']
    list_filter = ['connection_status', 'created_at']
    search_fields = ['user__username', 'user__email']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']
    fieldsets = (
        ('Account', {
            'fields': ('user', 'connection_status')
        }),
        ('Tokens', {
            'fields': ('api_key', 'token_expires_at')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )
