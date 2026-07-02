# posts/admin.py
from django.contrib import admin
from .models import PostJob, PostLog

@admin.register(PostJob)
class PostJobAdmin(admin.ModelAdmin):
    list_display = ['id', 'news', 'platform', 'status', 'retry_count', 'created_at']
    list_filter = ['status', 'platform']
    search_fields = ['news__title']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']

@admin.register(PostLog)
class PostLogAdmin(admin.ModelAdmin):
    list_display = ['job', 'action', 'status', 'created_at']
    list_filter = ['status', 'action']
    readonly_fields = ['created_at']
    ordering = ['-created_at']