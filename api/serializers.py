# api/serializers.py
from rest_framework import serializers
from django.contrib.auth.models import User
from news.models import News, Category, Country, NewsFilterRule
from social.models import TelegramChannel, SocialPlatform
from posts.models import PostJob, PostLog
from datetime import datetime

# ============ NEWS SERIALIZERS ============

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ['id', 'name', 'enabled', 'created_at']
        read_only_fields = ['created_at']


class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['id', 'name', 'code', 'enabled', 'created_at']
        read_only_fields = ['created_at']


class NewsSerializer(serializers.ModelSerializer):
    """Serializer for news articles"""
    
    posted_to = serializers.SerializerMethodField()
    
    class Meta:
        model = News
        fields = [
            'id', 'api_news_id', 'title', 'summary', 'link', 
            'image', 'published', 'category', 'country', 'source',
            'fetched_at', 'is_processed', 'posted_to'
        ]
        read_only_fields = ['id', 'api_news_id', 'fetched_at']
    
    def get_posted_to(self, obj):
        """Get list of platforms this news was posted to"""
        return list(obj.post_jobs.filter(
            status='success'
        ).values_list('platform__name', flat=True).distinct())


class NewsDetailSerializer(NewsSerializer):
    """Detailed news serializer with post jobs"""
    
    post_jobs = serializers.SerializerMethodField()
    
    class Meta(NewsSerializer.Meta):
        fields = NewsSerializer.Meta.fields + ['post_jobs']
    
    def get_post_jobs(self, obj):
        jobs = obj.post_jobs.all().order_by('-created_at')[:10]
        return PostJobSerializer(jobs, many=True).data


class NewsFilterRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsFilterRule
        fields = ['id', 'rule_type', 'rule_action', 'value', 'enabled', 'created_at']
        read_only_fields = ['created_at']


# ============ SOCIAL PLATFORM SERIALIZERS ============

class TelegramChannelSerializer(serializers.ModelSerializer):
    """Serializer for Telegram channels"""
    
    status = serializers.SerializerMethodField()
    
    class Meta:
        model = TelegramChannel
        fields = [
            'id', 'name', 'channel_username', 'channel_chat_id',
            'bot_token', 'bot_username', 'enabled', 'is_verified',
            'default_hashtags', 'parse_mode', 'subscriber_count',
            'status', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'is_verified', 'subscriber_count']
        extra_kwargs = {
            'bot_token': {'write_only': True},  # Hide bot token in responses
        }
    
    def get_status(self, obj):
        """Get channel status"""
        if obj.is_verified and obj.enabled:
            return 'connected'
        elif obj.enabled and not obj.is_verified:
            return 'pending_verification'
        else:
            return 'disabled'


class TelegramChannelTestSerializer(serializers.Serializer):
    """Serializer for testing Telegram connection"""
    bot_token = serializers.CharField(max_length=150)
    channel_chat_id = serializers.CharField(max_length=50, required=False)
    channel_username = serializers.CharField(max_length=50, required=False)


class SocialPlatformSerializer(serializers.ModelSerializer):
    """Serializer for social platforms"""
    
    status = serializers.SerializerMethodField()
    
    class Meta:
        model = SocialPlatform
        fields = ['id', 'name', 'enabled', 'config', 'status', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']
    
    def get_status(self, obj):
        """Get platform connection status"""
        if obj.name == 'telegram':
            has_verified = TelegramChannel.objects.filter(
                enabled=True, is_verified=True
            ).exists()
            return 'connected' if has_verified and obj.enabled else 'disconnected'
        return 'configured' if obj.enabled else 'disabled'


# ============ POST JOB SERIALIZERS ============

class PostJobSerializer(serializers.ModelSerializer):
    """Serializer for post jobs"""
    
    news_title = serializers.CharField(source='news.title', read_only=True)
    platform_name = serializers.CharField(source='platform.get_name_display', read_only=True)
    channel_name = serializers.SerializerMethodField()
    time_ago = serializers.SerializerMethodField()
    
    class Meta:
        model = PostJob
        fields = [
            'id', 'news', 'news_title', 'platform', 'platform_name',
            'telegram_channel', 'channel_name', 'status', 'posted_at',
            'response', 'message_id', 'retry_count', 'max_retries',
            'next_retry_at', 'last_error', 'time_ago', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'news', 'platform', 'telegram_channel', 'status',
            'posted_at', 'response', 'message_id', 'retry_count',
            'next_retry_at', 'last_error', 'created_at', 'updated_at'
        ]
    
    def get_channel_name(self, obj):
        """Get channel name for Telegram posts"""
        if obj.telegram_channel:
            return obj.telegram_channel.name
        return None
    
    def get_time_ago(self, obj):
        """Get human-readable time since creation"""
        from django.utils import timesince
        return timesince.timesince(obj.created_at)


class PostJobCreateSerializer(serializers.Serializer):
    """Serializer for creating manual post jobs"""
    news_id = serializers.IntegerField()
    platform_id = serializers.IntegerField()
    telegram_channel_id = serializers.IntegerField(required=False)
    
    def validate(self, data):
        """Validate the data"""
        # Check if news exists
        try:
            news = News.objects.get(id=data['news_id'])
        except News.DoesNotExist:
            raise serializers.ValidationError({"news_id": "News not found"})
        
        # Check if platform exists and is enabled
        try:
            platform = SocialPlatform.objects.get(id=data['platform_id'], enabled=True)
        except SocialPlatform.DoesNotExist:
            raise serializers.ValidationError({"platform_id": "Platform not found or disabled"})
        
        # If platform is Telegram, check channel
        if platform.name == 'telegram':
            if not data.get('telegram_channel_id'):
                raise serializers.ValidationError({
                    "telegram_channel_id": "Required for Telegram platform"
                })
            try:
                TelegramChannel.objects.get(
                    id=data['telegram_channel_id'],
                    enabled=True,
                    is_verified=True
                )
            except TelegramChannel.DoesNotExist:
                raise serializers.ValidationError({
                    "telegram_channel_id": "Telegram channel not found or not verified"
                })
        
        return data


class PostLogSerializer(serializers.ModelSerializer):
    """Serializer for post logs"""
    
    job_id = serializers.IntegerField(source='job.id', read_only=True)
    
    class Meta:
        model = PostLog
        fields = ['id', 'job', 'job_id', 'action', 'status', 'message', 'created_at']
        read_only_fields = ['created_at']


# ============ STATS SERIALIZERS ============

class DashboardStatsSerializer(serializers.Serializer):
    """Serializer for dashboard statistics"""
    
    total_news = serializers.IntegerField()
    news_today = serializers.IntegerField()
    total_posts = serializers.IntegerField()
    posts_today = serializers.IntegerField()
    pending_posts = serializers.IntegerField()
    failed_posts = serializers.IntegerField()
    success_rate = serializers.FloatField()
    last_run = serializers.DateTimeField(allow_null=True)
    next_run = serializers.DateTimeField(allow_null=True)
    
    platforms = serializers.DictField()
    categories = serializers.DictField()


class PlatformStatsSerializer(serializers.Serializer):
    """Serializer for platform statistics"""
    
    platform = serializers.CharField()
    total_posts = serializers.IntegerField()
    success_posts = serializers.IntegerField()
    failed_posts = serializers.IntegerField()
    success_rate = serializers.FloatField()
    last_post = serializers.DateTimeField(allow_null=True)
    pending = serializers.IntegerField()


# ============ USER SERIALIZERS ============

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_staff']
        read_only_fields = ['id', 'is_staff']


class UserLoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)


# ============ SETTINGS SERIALIZERS ============

class SystemSettingsSerializer(serializers.Serializer):
    """System settings serializer"""
    
    posting_interval = serializers.IntegerField(default=60, min_value=5, max_value=1440)
    max_posts_per_run = serializers.IntegerField(default=10, min_value=1, max_value=50)
    auto_post_enabled = serializers.BooleanField(default=True)
    default_hashtags = serializers.CharField(default='#News #BreakingNews')
    post_template = serializers.CharField(
        default="""
📰 {title}

{summary}

Read More👇
{link}

{hashtags}
        """
    )