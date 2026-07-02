# api/views.py
from rest_framework import viewsets, generics, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Count, Q, Avg, Sum, F
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import authenticate, login, logout
from datetime import datetime, timedelta
from django_celery_beat.models import PeriodicTask, IntervalSchedule
import json

from news.models import News, Category, Country, NewsFilterRule
from social.models import TelegramChannel, SocialPlatform
from posts.models import PostJob, PostLog
from posts.tasks import process_post_job
from news.tasks import fetch_and_queue_news
from social.telegram import TelegramService

from .serializers import *


from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiExample
from drf_spectacular.types import OpenApiTypes
from rest_framework_simplejwt.tokens import RefreshToken



# ============ AUTH VIEWS ============

class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = UserLoginSerializer(data=request.data)

        serializer.is_valid(raise_exception=True)

        user = authenticate(
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )

        if user is None:
            return Response(
                {"error": "Invalid credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        refresh = RefreshToken.for_user(user)

        return Response({
            "user": UserSerializer(user).data,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        })
    

class LogoutView(APIView):
    def post(self, request):
        logout(request)
        return Response({'message': 'Logged out successfully'})


class UserView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


# ============ NEWS VIEWS ============

@extend_schema_view(
    list=extend_schema(
        summary="List all news articles",
        description="Get a paginated list of news articles with optional filters",
        parameters=[
            OpenApiParameter(name='category', description='Filter by category', required=False, type=str),
            OpenApiParameter(name='country', description='Filter by country', required=False, type=str),
            OpenApiParameter(name='source', description='Filter by source', required=False, type=str),
            OpenApiParameter(name='search', description='Search in title and summary', required=False, type=str),
            OpenApiParameter(name='processed', description='Filter by processed status', required=False, type=bool),
            OpenApiParameter(name='date_from', description='Filter from date (ISO format)', required=False, type=str),
            OpenApiParameter(name='date_to', description='Filter to date (ISO format)', required=False, type=str),
        ],
        responses={200: NewsSerializer(many=True)}
    ),
    retrieve=extend_schema(
        summary="Get a single news article",
        description="Retrieve detailed information about a specific news article",
        responses={200: NewsDetailSerializer}
    ),
    fetch=extend_schema(
        summary="Fetch latest news",
        description="Trigger a manual fetch of latest news from Adekunle Report API",
        responses={202: {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'message': {'type': 'string'}}}}
    ),
    repost=extend_schema(
        summary="Repost a news article",
        description="Manually repost a specific news article to all enabled platforms",
        responses={200: {'type': 'object', 'properties': {'message': {'type': 'string'}, 'job_ids': {'type': 'array'}}}}
    ),
)

class NewsViewSet(viewsets.ModelViewSet):
    """ViewSet for news articles"""
    
    queryset = News.objects.all().order_by('-published')
    serializer_class = NewsSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return NewsDetailSerializer
        return NewsSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Filter by category
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)
        
        # Filter by country
        country = self.request.query_params.get('country')
        if country:
            queryset = queryset.filter(country=country)
        
        # Filter by source
        source = self.request.query_params.get('source')
        if source:
            queryset = queryset.filter(source=source)
        
        # Filter by processed status
        processed = self.request.query_params.get('processed')
        if processed is not None:
            queryset = queryset.filter(is_processed=processed.lower() == 'true')
        
        # Search
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) |
                Q(summary__icontains=search) |
                Q(source__icontains=search)
            )
        
        # Date range
        date_from = self.request.query_params.get('date_from')
        if date_from:
            try:
                date_from = datetime.fromisoformat(date_from)
                queryset = queryset.filter(published__gte=date_from)
            except ValueError:
                pass
        
        date_to = self.request.query_params.get('date_to')
        if date_to:
            try:
                date_to = datetime.fromisoformat(date_to)
                queryset = queryset.filter(published__lte=date_to)
            except ValueError:
                pass
        
        return queryset
    
    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get all categories with counts"""
        categories = News.objects.values('category').annotate(
            count=Count('id')
        ).order_by('-count')
        return Response(categories)
    
    @action(detail=False, methods=['get'])
    def countries(self, request):
        """Get all countries with counts"""
        countries = News.objects.values('country').annotate(
            count=Count('id')
        ).exclude(country__isnull=True).exclude(country='').order_by('-count')
        return Response(countries)
    
    @action(detail=False, methods=['get'])
    def sources(self, request):
        """Get all sources with counts"""
        sources = News.objects.values('source').annotate(
            count=Count('id')
        ).order_by('-count')
        return Response(sources)
    
    @action(detail=False, methods=['post'])
    def fetch(self, request):
        """Trigger manual news fetch"""
        result = fetch_and_queue_news.delay()
        return Response({
            'task_id': result.id,
            'message': 'News fetch started'
        })
    
    @action(detail=True, methods=['post'])
    def repost(self, request, pk=None):
        """Manually repost a news article"""
        news = self.get_object()
        
        # Get enabled platforms
        platforms = SocialPlatform.objects.filter(enabled=True)
        if not platforms.exists():
            return Response(
                {'error': 'No enabled platforms found'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        jobs_created = []
        for platform in platforms:
            if platform.name == 'telegram':
                channels = TelegramChannel.objects.filter(enabled=True, is_verified=True)
                for channel in channels:
                    job = PostJob.objects.create(
                        news=news,
                        platform=platform,
                        telegram_channel=channel,
                        status='pending'
                    )
                    jobs_created.append(job.id)
            else:
                job = PostJob.objects.create(
                    news=news,
                    platform=platform,
                    status='pending'
                )
                jobs_created.append(job.id)
        
        return Response({
            'message': f'Created {len(jobs_created)} repost jobs',
            'job_ids': jobs_created
        })


# ============ CATEGORY VIEWS ============

class CategoryViewSet(viewsets.ModelViewSet):
    """ViewSet for categories"""
    
    queryset = Category.objects.all().order_by('name')
    serializer_class = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def enabled(self, request):
        """Get only enabled categories"""
        categories = Category.objects.filter(enabled=True)
        return Response(CategorySerializer(categories, many=True).data)


# ============ COUNTRY VIEWS ============

class CountryViewSet(viewsets.ModelViewSet):
    """ViewSet for countries"""
    
    queryset = Country.objects.all().order_by('name')
    serializer_class = CountrySerializer
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def enabled(self, request):
        """Get only enabled countries"""
        countries = Country.objects.filter(enabled=True)
        return Response(CountrySerializer(countries, many=True).data)


# ============ FILTER RULE VIEWS ============

class FilterRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for news filter rules"""
    
    queryset = NewsFilterRule.objects.all().order_by('rule_type', 'value')
    serializer_class = NewsFilterRuleSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def enabled(self, request):
        """Get only enabled rules"""
        rules = NewsFilterRule.objects.filter(enabled=True)
        return Response(NewsFilterRuleSerializer(rules, many=True).data)


# ============ TELEGRAM VIEWS ============

@extend_schema_view(
    list=extend_schema(
        summary="List Telegram channels",
        description="Get all configured Telegram channels"
    ),
    create=extend_schema(
        summary="Create a Telegram channel",
        description="Add a new Telegram channel configuration",
        examples=[
            OpenApiExample(
                'Example Request',
                value={
                    'name': 'Ubuntu News',
                    'channel_username': '@UbuntuNews',
                    'channel_chat_id': '-1001234567890',
                    'bot_token': '1234567890:ABCdefGHIjklMNOpqrsTUVwxyz',
                    'default_hashtags': '#News #BreakingNews'
                }
            )
        ]
    ),
    test=extend_schema(
        summary="Test Telegram connection",
        description="Test bot token and channel access",
        request=TelegramChannelTestSerializer
    ),
    verify=extend_schema(
        summary="Verify Telegram channel",
        description="Verify that the channel is accessible and bot has permissions",
        responses={200: {'type': 'object', 'properties': {'success': {'type': 'boolean'}, 'message': {'type': 'string'}}}}
    ),
    test_post=extend_schema(
        summary="Send test post",
        description="Send a test message to verify posting works"
    ),
)

class TelegramChannelViewSet(viewsets.ModelViewSet):
    """ViewSet for Telegram channels"""
    
    queryset = TelegramChannel.objects.all().order_by('-created_at')
    serializer_class = TelegramChannelSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['post'])
    def test(self, request):
        """Test a Telegram connection"""
        serializer = TelegramChannelTestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        bot_token = data['bot_token']
        
        # Test bot token
        service = TelegramService(bot_token)
        success, result = service.test_connection()
        
        if not success:
            return Response({
                'success': False,
                'error': f'Invalid bot token: {result}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get chat info if chat_id provided
        chat_id = data.get('channel_chat_id')
        if chat_id:
            success, chat_info = service.get_channel_info(chat_id)
            if success:
                return Response({
                    'success': True,
                    'bot_info': result,
                    'chat_info': chat_info,
                    'message': 'Connection successful'
                })
            else:
                return Response({
                    'success': False,
                    'error': chat_info,
                    'bot_info': result
                }, status=status.HTTP_400_BAD_REQUEST)
        
        return Response({
            'success': True,
            'bot_info': result,
            'message': 'Bot token is valid. Add channel chat ID to verify channel access.'
        })
    
    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        """Verify a Telegram channel connection"""
        channel = self.get_object()
        
        # Test connection
        service = TelegramService(channel.bot_token)
        success, result = service.test_connection()
        
        if not success:
            return Response({
                'success': False,
                'error': f'Invalid bot token: {result}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Test channel access
        success, chat_info = service.get_channel_info(channel.channel_chat_id)
        if not success:
            return Response({
                'success': False,
                'error': f'Cannot access channel: {chat_info}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Update channel info
        channel.is_verified = True
        channel.bot_username = result.get('username', channel.bot_username)
        channel.subscriber_count = chat_info.get('member_count', 0)
        channel.save()
        
        return Response({
            'success': True,
            'message': 'Channel verified successfully',
            'channel_info': chat_info
        })
    
    @action(detail=True, methods=['post'])
    def test_post(self, request, pk=None):
        """Send a test post to verify channel"""
        channel = self.get_object()
        
        if not channel.is_verified:
            return Response({
                'error': 'Channel is not verified. Verify first.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        service = TelegramService(channel.bot_token)
        test_message = """
🔍 <b>Test Post</b>

This is a test message to verify your Telegram channel integration.

✅ Your bot is working correctly!
        """
        
        response = service.send_message(
            chat_id=channel.get_chat_id(),
            text=test_message,
            parse_mode=channel.parse_mode
        )
        
        if response.get('ok'):
            return Response({
                'success': True,
                'message': 'Test post sent successfully',
                'message_id': response['result']['message_id']
            })
        else:
            return Response({
                'success': False,
                'error': response.get('description', 'Unknown error')
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def sync_info(self, request, pk=None):
        """Sync channel info (subscriber count, etc.)"""
        channel = self.get_object()
        
        if not channel.is_verified:
            return Response({
                'error': 'Channel is not verified'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        service = TelegramService(channel.bot_token)
        success, chat_info = service.get_channel_info(channel.channel_chat_id)
        
        if success:
            channel.subscriber_count = chat_info.get('member_count', 0)
            channel.save()
            
            return Response({
                'success': True,
                'channel_info': chat_info
            })
        else:
            return Response({
                'success': False,
                'error': chat_info
            }, status=status.HTTP_400_BAD_REQUEST)


# ============ SOCIAL PLATFORM VIEWS ============

class SocialPlatformViewSet(viewsets.ModelViewSet):
    """ViewSet for social platforms"""
    
    queryset = SocialPlatform.objects.all()
    serializer_class = SocialPlatformSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    @action(detail=False, methods=['get'])
    def enabled(self, request):
        """Get only enabled platforms"""
        platforms = SocialPlatform.objects.filter(enabled=True)
        return Response(SocialPlatformSerializer(platforms, many=True).data)
    
    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle platform enabled/disabled"""
        platform = self.get_object()
        platform.enabled = not platform.enabled
        platform.save()
        return Response(SocialPlatformSerializer(platform).data)


# ============ POST JOB VIEWS ============

class PostJobViewSet(viewsets.ModelViewSet):
    """ViewSet for post jobs"""
    
    queryset = PostJob.objects.all().order_by('-created_at')
    serializer_class = PostJobSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by platform
        platform = self.request.query_params.get('platform')
        if platform:
            queryset = queryset.filter(platform__name=platform)
        
        # Filter by news
        news_id = self.request.query_params.get('news_id')
        if news_id:
            queryset = queryset.filter(news_id=news_id)
        
        # Date range
        date_from = self.request.query_params.get('date_from')
        if date_from:
            try:
                date_from = datetime.fromisoformat(date_from)
                queryset = queryset.filter(created_at__gte=date_from)
            except ValueError:
                pass
        
        date_to = self.request.query_params.get('date_to')
        if date_to:
            try:
                date_to = datetime.fromisoformat(date_to)
                queryset = queryset.filter(created_at__lte=date_to)
            except ValueError:
                pass
        
        return queryset
    
    @action(detail=False, methods=['post'])
    def create_manual(self, request):
        """Create a manual post job"""
        serializer = PostJobCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        news = News.objects.get(id=data['news_id'])
        platform = SocialPlatform.objects.get(id=data['platform_id'])
        
        with transaction.atomic():
            if platform.name == 'telegram':
                channel = TelegramChannel.objects.get(id=data['telegram_channel_id'])
                job = PostJob.objects.create(
                    news=news,
                    platform=platform,
                    telegram_channel=channel,
                    status='pending'
                )
            else:
                job = PostJob.objects.create(
                    news=news,
                    platform=platform,
                    status='pending'
                )
        
        # Process immediately
        process_post_job.delay(job.id)
        
        return Response({
            'message': 'Job created and processing',
            'job_id': job.id
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        """Retry a failed job"""
        job = self.get_object()
        
        if job.status not in ['failed', 'permanent_fail']:
            return Response({
                'error': 'Only failed jobs can be retried'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        job.status = 'pending'
        job.retry_count = 0
        job.last_error = None
        job.next_retry_at = None
        job.save()
        
        process_post_job.delay(job.id)
        
        return Response({
            'message': 'Job queued for retry',
            'job_id': job.id
        })
    
    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """Get logs for a job"""
        job = self.get_object()
        logs = job.logs.all().order_by('-created_at')
        return Response(PostLogSerializer(logs, many=True).data)


# ============ STATS VIEWS ============

class DashboardStatsView(APIView):
    """Get dashboard statistics"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        today = timezone.now().date()
        today_start = timezone.make_aware(
            datetime.combine(today, datetime.min.time())
        )
        tomorrow_start = today_start + timedelta(days=1)
        
        # News stats
        total_news = News.objects.count()
        news_today = News.objects.filter(fetched_at__gte=today_start).count()
        
        # Post stats
        total_posts = PostJob.objects.count()
        posts_today = PostJob.objects.filter(created_at__gte=today_start).count()
        pending_posts = PostJob.objects.filter(status='pending').count()
        failed_posts = PostJob.objects.filter(status__in=['failed', 'permanent_fail']).count()
        success_posts = PostJob.objects.filter(status='success').count()
        
        success_rate = (success_posts / total_posts * 100) if total_posts > 0 else 0
        
        # Platform stats
        platforms = {}
        for platform in SocialPlatform.objects.all():
            platform_jobs = PostJob.objects.filter(platform=platform)
            platform_success = platform_jobs.filter(status='success').count()
            platform_total = platform_jobs.count()
            
            platforms[platform.name] = {
                'total': platform_total,
                'success': platform_success,
                'failed': platform_jobs.filter(status__in=['failed', 'permanent_fail']).count(),
                'pending': platform_jobs.filter(status='pending').count(),
                'success_rate': (platform_success / platform_total * 100) if platform_total > 0 else 0
            }
        
        # Category stats
        categories = {}
        for category in Category.objects.filter(enabled=True):
            count = News.objects.filter(category=category.name).count()
            if count > 0:
                categories[category.name] = count
        
        # Last run info
        last_job = PostJob.objects.order_by('-created_at').first()
        
        # Get next scheduled run
        next_run = None
        try:
            schedule = IntervalSchedule.objects.first()
            if schedule:
                next_run = timezone.now() + timedelta(
                    minutes=schedule.every if schedule.period == 'minutes' else schedule.every * 60
                )
        except:
            pass
        
        data = {
            'total_news': total_news,
            'news_today': news_today,
            'total_posts': total_posts,
            'posts_today': posts_today,
            'pending_posts': pending_posts,
            'failed_posts': failed_posts,
            'success_rate': round(success_rate, 2),
            'last_run': last_job.created_at if last_job else None,
            'next_run': next_run,
            'platforms': platforms,
            'categories': categories,
        }
        
        return Response(data)


class PlatformStatsView(APIView):
    """Get platform-specific statistics"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        stats = []
        
        for platform in SocialPlatform.objects.all():
            platform_jobs = PostJob.objects.filter(platform=platform)
            total = platform_jobs.count()
            success = platform_jobs.filter(status='success').count()
            failed = platform_jobs.filter(status__in=['failed', 'permanent_fail']).count()
            pending = platform_jobs.filter(status='pending').count()
            last_post = platform_jobs.filter(status='success').order_by('-posted_at').first()
            
            stats.append({
                'platform': platform.name,
                'total_posts': total,
                'success_posts': success,
                'failed_posts': failed,
                'pending': pending,
                'success_rate': round((success / total * 100) if total > 0 else 0, 2),
                'last_post': last_post.posted_at if last_post else None
            })
        
        return Response(stats)


# ============ SETTINGS VIEWS ============

class SystemSettingsView(APIView):
    """Manage system settings"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        """Get current settings"""
        try:
            # Get or create settings from celery beat
            schedule = IntervalSchedule.objects.first()
            if not schedule:
                schedule = IntervalSchedule.objects.create(
                    every=60,
                    period=IntervalSchedule.MINUTES
                )
            
            # Get or create periodic task
            task = PeriodicTask.objects.filter(name='Fetch News').first()
            if not task:
                task = PeriodicTask.objects.create(
                    name='Fetch News',
                    task='news.tasks.fetch_and_queue_news',
                    interval=schedule,
                    enabled=True
                )
            
            settings_data = {
                'posting_interval': schedule.every,
                'auto_post_enabled': task.enabled,
                'max_posts_per_run': 10,  # Could be stored in a settings model
                'default_hashtags': '#News #BreakingNews',
                'post_template': """
📰 {title}

{summary}

Read More👇
{link}

{hashtags}
                """
            }
        except:
            settings_data = {
                'posting_interval': 60,
                'auto_post_enabled': True,
                'max_posts_per_run': 10,
                'default_hashtags': '#News #BreakingNews',
                'post_template': """
📰 {title}

{summary}

Read More👇
{link}

{hashtags}
                """
            }
        
        return Response(settings_data)
    
    def post(self, request):
        """Update settings"""
        data = request.data
        
        try:
            # Update interval schedule
            schedule = IntervalSchedule.objects.first()
            if not schedule:
                schedule = IntervalSchedule.objects.create(
                    every=data.get('posting_interval', 60),
                    period=IntervalSchedule.MINUTES
                )
            else:
                schedule.every = data.get('posting_interval', 60)
                schedule.save()
            
            # Update periodic task
            task = PeriodicTask.objects.filter(name='Fetch News').first()
            if task:
                task.interval = schedule
                task.enabled = data.get('auto_post_enabled', True)
                task.save()
            
            return Response({
                'message': 'Settings updated successfully',
                'settings': data
            })
            
        except Exception as e:
            return Response({
                'error': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)