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
from social.models import BufferAccount, TelegramChannel, SocialPlatform
from posts.models import PostJob, PostLog
from posts.tasks import process_post_job
from news.tasks import fetch_and_queue_news
from social.buffer import BufferAuthenticationError, BufferService, BufferServiceError
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


# ============ BUFFER VIEWS ============

class BufferAPIView(APIView):
    """Base helpers for authenticated Buffer API views."""

    permission_classes = [permissions.IsAuthenticated]

    def get_buffer_account(self):
        return BufferAccount.objects.filter(user=self.request.user).order_by('-created_at').first()

    def buffer_error_response(self, error):
        if isinstance(error, BufferAuthenticationError):
            response_status = status.HTTP_401_UNAUTHORIZED
        else:
            response_status = status.HTTP_400_BAD_REQUEST

        return Response({
            'success': False,
            'error': str(error)
        }, status=response_status)


class BufferConnectView(BufferAPIView):
    @extend_schema(
        summary="Connect Buffer",
        description="Connect the authenticated user to Buffer using Buffer credentials.",
        request=BufferConnectSerializer,
        responses={200: BufferAccountStatusSerializer}
    )
    def post(self, request):
        serializer = BufferConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        service = BufferService(data['access_token'])

        try:
            service.test_connection()
        except BufferServiceError as error:
            return self.buffer_error_response(error)

        account = self.get_buffer_account()
        if account is None:
            account = BufferAccount(user=request.user)

        account.access_token = data['access_token']
        account.refresh_token = data.get('refresh_token') or None
        account.token_expires_at = data.get('token_expires_at')
        account.connection_status = 'connected'
        account.save()

        return Response({
            'success': True,
            'account': BufferAccountStatusSerializer(account).data
        })


class BufferStatusView(BufferAPIView):
    @extend_schema(
        summary="Get Buffer status",
        description="Return the authenticated user's Buffer connection status.",
        responses={200: BufferAccountStatusSerializer}
    )
    def get(self, request):
        account = self.get_buffer_account()
        if account is None:
            return Response({
                'connected': False,
                'connection_status': 'disconnected'
            })

        return Response(BufferAccountStatusSerializer(account).data)


class BufferProfilesView(BufferAPIView):
    @extend_schema(
        summary="List Buffer profiles",
        description="Fetch Buffer profiles available to the connected Buffer account.",
        responses={200: BufferProfileSerializer(many=True)}
    )
    def get(self, request):
        account = self.get_buffer_account()
        if account is None or account.connection_status != 'connected':
            return Response({
                'error': 'Buffer is not connected'
            }, status=status.HTTP_400_BAD_REQUEST)

        service = BufferService(account.access_token)

        try:
            profiles = service.get_profiles()
        except BufferServiceError as error:
            return self.buffer_error_response(error)

        return Response({
            'profiles': profiles
        })


class BufferDisconnectView(BufferAPIView):
    @extend_schema(
        summary="Disconnect Buffer",
        description="Remove stored Buffer credentials for the authenticated user.",
        responses={200: {'type': 'object'}}
    )
    def post(self, request):
        deleted_count, _ = BufferAccount.objects.filter(user=request.user).delete()

        return Response({
            'success': True,
            'connected': False,
            'connection_status': 'disconnected',
            'deleted_accounts': deleted_count
        })


class BufferTestView(BufferAPIView):
    @extend_schema(
        summary="Test Buffer connection",
        description="Test a supplied Buffer access token or the authenticated user's stored Buffer token.",
        request=BufferTestSerializer,
        responses={200: {'type': 'object'}}
    )
    def post(self, request):
        serializer = BufferTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        access_token = serializer.validated_data.get('access_token')
        if not access_token:
            account = self.get_buffer_account()
            if account is None:
                return Response({
                    'success': False,
                    'error': 'Buffer is not connected and no access_token was provided'
                }, status=status.HTTP_400_BAD_REQUEST)
            access_token = account.access_token

        service = BufferService(access_token)

        try:
            user_info = service.test_connection()
        except BufferServiceError as error:
            return self.buffer_error_response(error)

        return Response({
            'success': True,
            'message': 'Buffer connection successful',
            'user': user_info
        })


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





# ============ SOCIAL PLATFORM VIEWS ============

@extend_schema_view(
    list=extend_schema(
        summary="List all social platforms",
        description="Get a list of all configured social media platforms with their status",
        responses={200: SocialPlatformSerializer(many=True)}
    ),
    create=extend_schema(
        summary="Add a new social platform",
        description="Register a new social media platform for posting news",
        examples=[
            OpenApiExample(
                'Add Telegram Platform',
                value={
                    'name': 'telegram',
                    'enabled': True,
                    'config': {
                        'bot_token': '1234567890:ABCdefGHIjklMNOpqrsTUVwxyz',
                        'channel_chat_id': '-1001234567890'
                    }
                }
            ),
            OpenApiExample(
                'Add X (Twitter) Platform',
                value={
                    'name': 'twitter',
                    'enabled': True,
                    'config': {
                        'api_key': 'your_api_key',
                        'api_secret': 'your_api_secret',
                        'access_token': 'your_access_token',
                        'access_token_secret': 'your_access_token_secret'
                    }
                }
            ),
            OpenApiExample(
                'Add Facebook Platform',
                value={
                    'name': 'facebook',
                    'enabled': True,
                    'config': {
                        'page_id': 'your_page_id',
                        'access_token': 'your_facebook_access_token'
                    }
                }
            ),
            OpenApiExample(
                'Add Instagram Platform',
                value={
                    'name': 'instagram',
                    'enabled': True,
                    'config': {
                        'business_account_id': 'your_business_id',
                        'access_token': 'your_instagram_access_token'
                    }
                }
            )
        ]
    ),
    retrieve=extend_schema(
        summary="Get platform details",
        description="Retrieve detailed information about a specific social platform"
    ),
    update=extend_schema(
        summary="Update platform",
        description="Update an existing social platform configuration"
    ),
    partial_update=extend_schema(
        summary="Partial update platform",
        description="Partially update a social platform configuration"
    ),
    destroy=extend_schema(
        summary="Delete platform",
        description="Remove a social platform from the system"
    ),
    enabled=extend_schema(
        summary="Get enabled platforms",
        description="Get all enabled social media platforms"
    ),
    toggle=extend_schema(
        summary="Toggle platform status",
        description="Enable or disable a social media platform"
    ),
    connection_status=extend_schema(
        summary="Get platform connection status",
        description="Check the connection status of a specific platform"
    )
)
class SocialPlatformViewSet(viewsets.ModelViewSet):
    """ViewSet for managing social media platforms"""
    
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
        
        # Log the action
        PostLog.objects.create(
            job=None,  # No specific job
            action='platform_toggle',
            status='success' if platform.enabled else 'disabled',
            message=f"Platform {platform.name} {'enabled' if platform.enabled else 'disabled'}"
        )
        
        return Response({
            'message': f'Platform {platform.name} {"enabled" if platform.enabled else "disabled"}',
            'platform': SocialPlatformSerializer(platform).data
        })
    
    @action(detail=True, methods=['get'])
    def connection_status(self, request, pk=None):
        """Check the connection status of a platform"""
        platform = self.get_object()
        
        status_data = {
            'platform': platform.name,
            'enabled': platform.enabled,
            'is_connected': False,
            'details': None
        }
        
        # Check specific platform connections
        if platform.name == 'telegram':
            # Check if there are any verified Telegram channels
            has_verified = TelegramChannel.objects.filter(
                enabled=True, 
                is_verified=True
            ).exists()
            status_data['is_connected'] = has_verified
            status_data['details'] = {
                'verified_channels': TelegramChannel.objects.filter(
                    enabled=True, 
                    is_verified=True
                ).count(),
                'total_channels': TelegramChannel.objects.filter(enabled=True).count()
            }
        elif platform.name == 'twitter':
            # Check Twitter connection
            # TODO: Implement Twitter connection check
            status_data['is_connected'] = platform.config.get('access_token') is not None
            status_data['details'] = {
                'has_api_key': bool(platform.config.get('api_key')),
                'has_access_token': bool(platform.config.get('access_token'))
            }
        elif platform.name == 'facebook':
            # Check Facebook connection
            status_data['is_connected'] = platform.config.get('access_token') is not None
            status_data['details'] = {
                'has_page_id': bool(platform.config.get('page_id')),
                'has_access_token': bool(platform.config.get('access_token'))
            }
        elif platform.name == 'instagram':
            # Check Instagram connection
            status_data['is_connected'] = platform.config.get('access_token') is not None
            status_data['details'] = {
                'has_business_id': bool(platform.config.get('business_account_id')),
                'has_access_token': bool(platform.config.get('access_token'))
            }
        
        return Response(status_data)


# ============ DEDICATED PLATFORM CONFIG VIEWS ============

class PlatformConnectionView(APIView):
    """
    Dedicated view for connecting social media platforms
    """
    permission_classes = [permissions.IsAuthenticated]
    
    @extend_schema(
        summary="Connect a social platform",
        description="Configure and connect a social media platform with credentials",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'platform': {'type': 'string', 'enum': ['telegram', 'twitter', 'facebook', 'instagram']},
                    'config': {'type': 'object'},
                    'enabled': {'type': 'boolean', 'default': True}
                },
                'required': ['platform', 'config']
            }
        },
        examples=[
            OpenApiExample(
                'Connect Telegram',
                value={
                    'platform': 'telegram',
                    'enabled': True,
                    'config': {
                        'bot_token': '1234567890:ABCdefGHIjklMNOpqrsTUVwxyz',
                        'channel_chat_id': '-1001234567890'
                    }
                }
            ),
            OpenApiExample(
                'Connect Twitter/X',
                value={
                    'platform': 'twitter',
                    'enabled': True,
                    'config': {
                        'api_key': 'your_api_key',
                        'api_secret': 'your_api_secret',
                        'access_token': 'your_access_token',
                        'access_token_secret': 'your_access_token_secret'
                    }
                }
            )
        ]
    )
    def post(self, request):
        """Connect a new social media platform"""
        platform_name = request.data.get('platform')
        config = request.data.get('config', {})
        enabled = request.data.get('enabled', True)
        
        # Validate platform
        valid_platforms = ['telegram', 'twitter', 'facebook', 'instagram']
        if platform_name not in valid_platforms:
            return Response({
                'error': f'Invalid platform. Must be one of: {", ".join(valid_platforms)}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if platform already exists
        if SocialPlatform.objects.filter(name=platform_name).exists():
            return Response({
                'error': f'Platform {platform_name} already exists. Use update endpoint instead.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate Telegram configuration
        if platform_name == 'telegram':
            if not config.get('bot_token'):
                return Response({
                    'error': 'bot_token is required for Telegram'
                }, status=status.HTTP_400_BAD_REQUEST)
            if not config.get('channel_chat_id'):
                return Response({
                    'error': 'channel_chat_id is required for Telegram'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate Twitter configuration
        if platform_name == 'twitter':
            required_fields = ['api_key', 'api_secret', 'access_token', 'access_token_secret']
            missing_fields = [f for f in required_fields if not config.get(f)]
            if missing_fields:
                return Response({
                    'error': f'Missing required fields for Twitter: {", ".join(missing_fields)}'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate Facebook configuration
        if platform_name == 'facebook':
            if not config.get('page_id') and not config.get('access_token'):
                return Response({
                    'error': 'page_id and access_token are required for Facebook'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Validate Instagram configuration
        if platform_name == 'instagram':
            if not config.get('business_account_id') and not config.get('access_token'):
                return Response({
                    'error': 'business_account_id and access_token are required for Instagram'
                }, status=status.HTTP_400_BAD_REQUEST)
        
        # Create platform
        platform = SocialPlatform.objects.create(
            name=platform_name,
            enabled=enabled,
            config=config
        )
        
        # For Telegram, create a channel entry if bot_token is provided
        if platform_name == 'telegram' and config.get('bot_token'):
            telegram_channel = TelegramChannel.objects.create(
                name=f"Telegram Channel for {platform_name}",
                channel_username=config.get('channel_username', f'@{platform_name}_news'),
                channel_chat_id=config['channel_chat_id'],
                bot_token=config['bot_token'],
                default_hashtags='#News #BreakingNews',
                enabled=enabled,
                is_verified=False  # Needs verification
            )
            
            # Attempt to verify automatically
            service = TelegramService(config['bot_token'])
            success, chat_info = service.get_channel_info(config['channel_chat_id'])
            if success:
                telegram_channel.is_verified = True
                telegram_channel.subscriber_count = chat_info.get('member_count', 0)
                telegram_channel.save()
        
        # Log the action
        PostLog.objects.create(
            job=None,
            action='platform_connected',
            status='success',
            message=f'Connected {platform_name} platform'
        )
        
        return Response({
            'message': f'Platform {platform_name} connected successfully',
            'platform': SocialPlatformSerializer(platform).data
        }, status=status.HTTP_201_CREATED)
    
    @extend_schema(
        summary="Update platform connection",
        description="Update configuration for an existing platform",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'config': {'type': 'object'},
                    'enabled': {'type': 'boolean'}
                }
            }
        }
    )
    def put(self, request, platform_name):
        """Update platform configuration"""
        try:
            platform = SocialPlatform.objects.get(name=platform_name)
        except SocialPlatform.DoesNotExist:
            return Response({
                'error': f'Platform {platform_name} not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Update config if provided
        if 'config' in request.data:
            platform.config.update(request.data['config'])
        
        # Update enabled status if provided
        if 'enabled' in request.data:
            platform.enabled = request.data['enabled']
        
        platform.save()
        
        return Response({
            'message': f'Platform {platform_name} updated successfully',
            'platform': SocialPlatformSerializer(platform).data
        })
    
    @extend_schema(
        summary="Disconnect platform",
        description="Disconnect and optionally delete a platform"
    )
    def delete(self, request, platform_name):
        """Disconnect a platform"""
        try:
            platform = SocialPlatform.objects.get(name=platform_name)
            platform.delete()
            
            return Response({
                'message': f'Platform {platform_name} disconnected successfully'
            })
        except SocialPlatform.DoesNotExist:
            return Response({
                'error': f'Platform {platform_name} not found'
            }, status=status.HTTP_404_NOT_FOUND)


class PlatformTestView(APIView):
    """
    Test platform connection
    """
    permission_classes = [permissions.IsAuthenticated]
    
    @extend_schema(
        summary="Test platform connection",
        description="Test if a platform is properly configured and connected",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'platform': {'type': 'string', 'enum': ['telegram', 'twitter', 'facebook', 'instagram']},
                    'config': {'type': 'object'}
                },
                'required': ['platform']
            }
        }
    )
    def post(self, request):
        """Test a platform connection"""
        platform_name = request.data.get('platform')
        config = request.data.get('config', {})
        
        if not platform_name:
            return Response({
                'error': 'platform is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        result = {
            'platform': platform_name,
            'success': False,
            'message': '',
            'details': None
        }
        
        if platform_name == 'telegram':
            # Test Telegram connection
            bot_token = config.get('bot_token')
            if not bot_token:
                # Try to get from existing platform
                try:
                    platform = SocialPlatform.objects.get(name='telegram')
                    bot_token = platform.config.get('bot_token')
                except SocialPlatform.DoesNotExist:
                    return Response({
                        'error': 'bot_token is required for testing Telegram'
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            service = TelegramService(bot_token)
            success, info = service.test_connection()
            
            if success:
                result['success'] = True
                result['message'] = 'Bot connection successful'
                result['details'] = {
                    'bot_username': info.get('username'),
                    'bot_name': info.get('first_name'),
                    'is_bot': info.get('is_bot')
                }
                
                # Try to get channel info if chat_id provided
                chat_id = config.get('channel_chat_id')
                if chat_id:
                    success2, chat_info = service.get_channel_info(chat_id)
                    if success2:
                        result['details']['channel'] = {
                            'title': chat_info.get('title'),
                            'username': chat_info.get('username'),
                            'member_count': chat_info.get('member_count'),
                            'type': chat_info.get('type')
                        }
                    else:
                        result['message'] += ' (Channel access failed)'
                        result['details']['error'] = chat_info
            else:
                result['message'] = f'Connection failed: {info}'
        
        elif platform_name == 'twitter':
            # TODO: Implement Twitter test
            result['message'] = 'Twitter testing not yet implemented'
        
        elif platform_name == 'facebook':
            # TODO: Implement Facebook test
            result['message'] = 'Facebook testing not yet implemented'
        
        elif platform_name == 'instagram':
            # TODO: Implement Instagram test
            result['message'] = 'Instagram testing not yet implemented'
        
        else:
            return Response({
                'error': f'Invalid platform: {platform_name}'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        return Response(result)


# ============ BULK PLATFORM CONFIG VIEW ============

class BulkPlatformConfigView(APIView):
    """
    View for managing multiple platforms at once
    """
    permission_classes = [permissions.IsAuthenticated]
    
    @extend_schema(
        summary="Get all platform configurations",
        description="Get configuration for all social media platforms"
    )
    def get(self, request):
        """Get all platform configurations"""
        platforms = SocialPlatform.objects.all()
        data = {}
        
        for platform in platforms:
            # Hide sensitive data
            config = platform.config.copy()
            if 'bot_token' in config:
                config['bot_token'] = '******'  # Mask sensitive data
            if 'access_token' in config:
                config['access_token'] = '******'
            if 'api_secret' in config:
                config['api_secret'] = '******'
            if 'access_token_secret' in config:
                config['access_token_secret'] = '******'
            
            data[platform.name] = {
                'enabled': platform.enabled,
                'config': config
            }
        
        return Response(data)
    
    @extend_schema(
        summary="Bulk update platforms",
        description="Update multiple platform configurations at once"
    )
    def post(self, request):
        """Bulk update platform configurations"""
        data = request.data
        results = {}
        
        for platform_name, platform_data in data.items():
            try:
                platform = SocialPlatform.objects.get(name=platform_name)
                
                if 'enabled' in platform_data:
                    platform.enabled = platform_data['enabled']
                
                if 'config' in platform_data:
                    # Only update provided config fields
                    for key, value in platform_data['config'].items():
                        if value and value != '******':  # Don't overwrite with masked values
                            platform.config[key] = value
                
                platform.save()
                results[platform_name] = 'updated'
                
            except SocialPlatform.DoesNotExist:
                results[platform_name] = 'not_found'
            except Exception as e:
                results[platform_name] = f'error: {str(e)}'
        
        return Response({
            'message': 'Bulk update completed',
            'results': results
        })
