# api/views.py
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import authenticate, logout
from datetime import datetime, timedelta
from django_celery_beat.models import PeriodicTask, IntervalSchedule

import requests

from news.models import News, Category, Country, NewsFilterRule
from news.services import NewsFetcher
from social.models import BufferAccount, TelegramChannel, SocialPlatform
from posts.models import PostJob, PostLog
from posts.tasks import process_post_job
from news.tasks import fetch_and_queue_news
from social.buffer import BufferAuthenticationError, BufferService, BufferServiceError
from social.telegram import TelegramService

from .serializers import *


from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiExample
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

    def get_connected_buffer_account(self):
        account = self.get_buffer_account()
        if account is None or account.connection_status != 'connected':
            raise ValidationError({'buffer': 'Buffer is not connected'})
        return account

    def raise_buffer_error(self, error):
        if isinstance(error, BufferAuthenticationError):
            raise AuthenticationFailed(str(error))

        raise ValidationError({'buffer': str(error)})


class BufferConnectView(BufferAPIView):
    @extend_schema(
        summary="Connect Buffer",
        description="Connect the authenticated user to Buffer using a personal API key generated at Settings -> API.",
        request=BufferConnectSerializer,
        responses={200: BufferAccountStatusSerializer}
    )
    def post(self, request):
        serializer = BufferConnectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        service = BufferService(data['api_key'])

        try:
            service.test_connection()
        except BufferServiceError as error:
            self.raise_buffer_error(error)

        account = self.get_buffer_account()
        if account is None:
            account = BufferAccount(user=request.user)

        account.api_key = data['api_key']
        account.token_expires_at = data.get('token_expires_at')
        account.connection_status = 'connected'
        account.save()

        SocialPlatform.objects.get_or_create(
            name='buffer',
            defaults={'enabled': False},
        )

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


class BufferOrganizationsView(BufferAPIView):
    @extend_schema(
        summary="List Buffer organizations",
        description="Fetch the Buffer organizations available to the connected account's API key.",
        responses={200: {'type': 'object'}}
    )
    def get(self, request):
        account = self.get_connected_buffer_account()
        service = BufferService(account.api_key)

        try:
            organizations = service.get_organizations()
        except BufferServiceError as error:
            self.raise_buffer_error(error)

        return Response({
            'organizations': organizations
        })


class BufferChannelsView(BufferAPIView):
    """
    Lists channels (formerly 'profiles') for a Buffer organization.

    Buffer's new API scopes channels under an organization, so an
    organization_id is required. Pass it as a query param
    (?organization_id=...); if omitted, the user's first organization
    is used as a convenience default.
    """

    @extend_schema(
        summary="List Buffer channels",
        description="Fetch Buffer channels for an organization on the connected account.",
        parameters=[
            OpenApiParameter(
                name='organization_id',
                description='Buffer organization ID. Defaults to the first organization if omitted.',
                required=False,
                type=str,
            ),
        ],
        responses={200: BufferChannelSerializer(many=True)}
    )
    def get(self, request):
        account = self.get_connected_buffer_account()
        service = BufferService(account.api_key)

        organization_id = request.query_params.get('organization_id')

        try:
            if not organization_id:
                organizations = service.get_organizations()
                if not organizations:
                    raise ValidationError({'buffer': 'No Buffer organizations found for this account'})
                organization_id = organizations[0]['id']

            channels = service.get_channels(organization_id)
        except BufferServiceError as error:
            self.raise_buffer_error(error)

        return Response({
            'organization_id': organization_id,
            'channels': channels
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
        description="Test a supplied Buffer personal API key or the authenticated user's stored key.",
        request=BufferTestSerializer,
        responses={200: {'type': 'object'}}
    )
    def post(self, request):
        serializer = BufferTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        api_key = serializer.validated_data.get('api_key')
        if not api_key:
            account = self.get_buffer_account()
            if account is None:
                raise ValidationError({
                    'api_key': 'Buffer is not connected and no api_key was provided'
                })
            api_key = account.api_key

        service = BufferService(api_key)

        try:
            account_info = service.test_connection()
        except BufferServiceError as error:
            self.raise_buffer_error(error)

        return Response({
            'success': True,
            'message': 'Buffer connection successful',
            'account': account_info
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
        summary="Repost a news article via Buffer",
        description="Manually repost a specific news article through the authenticated user's connected Buffer account.",
        responses={200: {'type': 'object', 'properties': {'message': {'type': 'string'}, 'job_id': {'type': 'integer'}}}}
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

    @action(detail=False, methods=['post'])
    def fetch_recent(self, request):
        """
        Fetch news from the upstream API synchronously, deduplicate
        against existing News rows, and queue matching items for posting.

        Does not go through Celery for the fetch part, only for the
        individual post jobs.

        Returns a summary of what was fetched, saved, and queued.
        """
        fetcher = NewsFetcher()
        url = fetcher.API_URL

        total_fetched = 0
        new_count = 0
        duplicate_count = 0
        queued_count = 0
        pages_fetched = 0
        max_pages = 10

        while url and pages_fetched < max_pages:
            try:
                response = fetcher.session.get(url, timeout=30)
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.Timeout:
                return Response(
                    {"error": "Upstream API timed out after 30s"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            except requests.exceptions.RequestException as exc:
                return Response(
                    {"error": f"Upstream API request failed: {exc}"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            except ValueError:
                return Response(
                    {"error": "Upstream API returned invalid JSON"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            pages_fetched += 1
            results = data.get("results", [])
            total_fetched += len(results)

            if not results:
                break

            for item in results:
                api_id = item.get("id")
                if not api_id:
                    continue

                if News.objects.filter(api_news_id=api_id).exists():
                    duplicate_count += 1
                    continue

                news = News.create_from_api(item)
                new_count += 1

                if fetcher.should_post_news(news):
                    platforms = SocialPlatform.objects.filter(
                        enabled=True,
                        name__in=["telegram", "buffer"],
                    )

                    for platform in platforms:
                        if platform.name == "telegram":
                            channels = TelegramChannel.objects.filter(
                                enabled=True, is_verified=True
                            )
                            for channel in channels:
                                PostJob.objects.create(
                                    news=news,
                                    platform=platform,
                                    telegram_channel=channel,
                                    status="pending",
                                )
                                queued_count += 1
                        elif platform.name == "buffer":
                            buffer_accounts = BufferAccount.objects.filter(
                                connection_status="connected"
                            ).order_by("-updated_at")
                            for buffer_account in buffer_accounts:
                                PostJob.objects.create(
                                    news=news,
                                    platform=platform,
                                    buffer_account=buffer_account,
                                    status="pending",
                                )
                                queued_count += 1

            url = data.get("next")

        return Response({
            "fetched": total_fetched,
            "new": new_count,
            "duplicates": duplicate_count,
            "queued": queued_count,
            "pages_fetched": pages_fetched,
        })

    
    @action(detail=False, methods=["post"])
    def post_all(self, request):
        """
        Find all news that have never been successfully posted and that
        don't already have pending/processing jobs, and post up to
        `batch_size` of them immediately (no Celery).

        Accepts an optional `batch_size` query/body param (default 5) to
        control how many news items are posted per call.

        Returns a summary of what was posted / failed / skipped, plus how
        many eligible items remain for a follow-up call.
        """
        try:
            batch_size = int(request.data.get("batch_size") or request.query_params.get("batch_size") or 5)
        except (TypeError, ValueError):
            batch_size = 5
        batch_size = max(1, min(batch_size, 100))  # sane bounds

        successful_ids = PostJob.objects.filter(status="success").values("news_id")
        pending_ids_qs = PostJob.objects.filter(
            status__in=["pending", "processing"]
        ).values("news_id")

        base_qs = (
            News.objects.exclude(id__in=successful_ids)
            .exclude(id__in=pending_ids_qs)
            .order_by("-published")
        )

        total = base_qs.count()

        # Pre-load platforms / channels / accounts once.
        # Not filtered by `enabled` here — matches repost(), which posts
        # through a connected Buffer account regardless of the
        # SocialPlatform.enabled flag. Actual gating is done by whether
        # there's a verified channel / connected account below.
        platforms = list(
            SocialPlatform.objects.filter(name__in=["telegram", "buffer"])
        )
        telegram_channels = list(
            TelegramChannel.objects.filter(enabled=True, is_verified=True)
        )
        buffer_accounts = list(
            BufferAccount.objects.filter(connection_status="connected").order_by("-updated_at")
        )

        # Set of news IDs that have pending/processing jobs (safety check)
        pending_set = set(
            PostJob.objects.filter(status__in=["pending", "processing"])
            .values_list("news_id", flat=True)
            .distinct()
        )

        already_queued = 0
        posted_count = 0
        failed_count = 0
        processed_count = 0  # news items actually attempted this call

        for news in base_qs.iterator():
            if processed_count >= batch_size:
                break

            if news.id in pending_set:
                already_queued += 1
                continue

            processed_count += 1

            for platform in platforms:
                if platform.name == "telegram":
                    for channel in telegram_channels:
                        job = PostJob.objects.create(
                            news=news,
                            platform=platform,
                            telegram_channel=channel,
                            status="pending",
                        )
                        result = process_post_job(job.id)
                        if result.get("status") == "success":
                            posted_count += 1
                        else:
                            failed_count += 1
                elif platform.name == "buffer":
                    for buffer_account in buffer_accounts:
                        job = PostJob.objects.create(
                            news=news,
                            platform=platform,
                            buffer_account=buffer_account,
                            status="pending",
                        )
                        result = process_post_job(job.id)
                        if result.get("status") == "success":
                            posted_count += 1
                        else:
                            failed_count += 1

        remaining = total - already_queued - processed_count

        return Response({
            "total": total,
            "batch_size": batch_size,
            "processed": processed_count,
            "already_queued": already_queued,
            "posted": posted_count,
            "failed": failed_count,
            "remaining": max(remaining, 0),
        })
    
    @action(detail=True, methods=['post'])
    def repost(self, request, pk=None):
        """Repost a news article immediately (no Celery)."""
        news = self.get_object()

        buffer_account = BufferAccount.objects.filter(
            user=request.user,
            connection_status='connected'
        ).first()

        if not buffer_account:
            return Response(
                {'error': 'Buffer is not connected'},
                status=status.HTTP_400_BAD_REQUEST
            )

        platform, _ = SocialPlatform.objects.get_or_create(
            name='buffer',
            defaults={'enabled': False},
        )

        with transaction.atomic():
            job = PostJob.objects.create(
                news=news,
                platform=platform,
                buffer_account=buffer_account,
                status='pending'
            )

        result = process_post_job(job.id)

        if result.get('status') == 'success':
            return Response({
                'message': 'Reposted successfully',
                'job_id': job.id,
                'status': 'success',
            })
        else:
            return Response({
                'error': result.get('error', 'Repost failed'),
                'job_id': job.id,
                'status': 'failed',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
        serializer = PostJobCreateSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        
        data = serializer.validated_data
        news = data['news']
        platform = data['platform']
        
        with transaction.atomic():
            if platform.name == 'telegram':
                job = PostJob.objects.create(
                    news=news,
                    platform=platform,
                    telegram_channel=data['telegram_channel'],
                    status='pending'
                )
            elif platform.name == 'buffer':
                job = PostJob.objects.create(
                    news=news,
                    platform=platform,
                    buffer_account=data['buffer_account'],
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

        if (
            job.platform.name == 'buffer'
            and job.buffer_account
            and job.buffer_account.user_id != request.user.id
        ):
            raise PermissionDenied('You cannot retry a Buffer job for another user')
        
        if job.status not in ['failed', 'permanent_fail']:
            raise ValidationError({'status': 'Only failed jobs can be retried'})
        
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
        serializer = self.get_serializer(platforms, many=True)
        return Response(serializer.data)
    
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
            'platform': self.get_serializer(platform).data
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
        elif platform.name == 'buffer':
            connected_accounts = BufferAccount.objects.filter(
                user=request.user,
                connection_status='connected'
            )
            status_data['is_connected'] = connected_accounts.exists()
            status_data['details'] = {
                'connected_accounts': connected_accounts.count()
            }
        
        return Response(status_data)