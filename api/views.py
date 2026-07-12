# api/views.py
import concurrent
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from django.db import close_old_connections, transaction
from django.contrib.auth import authenticate, logout
from datetime import datetime, timedelta
from rest_framework.permissions import AllowAny

import requests

from news.models import News, Category, Country, NewsFilterRule
from news.services import NewsFetcher
from social.models import BufferAccount, TelegramChannel, SocialPlatform
from posts.models import PostJob, PostLog
from posts.tasks import process_post_job, task_lock, AlreadyRunning
from core.monitoring import monitor_post_job, connection_snapshot
from news.tasks import fetch_and_queue_news
from social.buffer import BufferAuthenticationError, BufferService, BufferServiceError
from social.telegram import TelegramService

from .serializers import *


from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter, OpenApiExample
from rest_framework_simplejwt.tokens import RefreshToken


def _should_post_news(news, rules):
    """Check if news should be posted based on pre-fetched filter rules.

    Identical logic to ``NewsFetcher.should_post_news`` but accepts a
    pre-fetched list of ``NewsFilterRule`` objects so it can be called
    inside loops without re-querying the database each time.
    """
    for rule in rules:
        value_lower = rule.value.lower()

        if rule.rule_type == 'category':
            match = news.category.lower() == value_lower
        elif rule.rule_type == 'country':
            match = (news.country or '').lower() == value_lower
        elif rule.rule_type == 'source':
            match = news.source.lower() == value_lower
        elif rule.rule_type == 'keyword':
            match = (
                value_lower in news.title.lower()
                or value_lower in (news.summary or '').lower()
            )
        else:
            continue

        if rule.rule_action == 'exclude' and match:
            return False

        if rule.rule_action == 'include' and not match:
            return False

    include_rules = [r for r in rules if r.rule_action == 'include']
    if include_rules:
        for rule in include_rules:
            if rule.rule_type == 'category' and news.category.lower() == rule.value.lower():
                return True
            if rule.rule_type == 'country' and (news.country or '').lower() == rule.value.lower():
                return True
            if rule.rule_type == 'source' and news.source.lower() == rule.value.lower():
                return True
            if rule.rule_type == 'keyword':
                if (rule.value.lower() in news.title.lower()
                        or rule.value.lower() in (news.summary or '').lower()):
                    return True
        return False

    return True




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

        # Check if an account with this API key already exists for this user
        existing = BufferAccount.objects.filter(
            user=request.user,
            api_key=data['api_key']
        ).first()

        if existing:
            # Update existing account
            account = existing
        else:
            # Create a new account
            account = BufferAccount(user=request.user)

        account.api_key = data['api_key']
        account.name = data.get('name', '')
        account.api_url = data['api_url']
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


class BufferAccountListView(BufferAPIView):
    """List all Buffer accounts for the current user with news/post counts."""

    @extend_schema(
        summary="List Buffer accounts",
        description="List all Buffer accounts for the authenticated user with news and post counts.",
        responses={200: BufferAccountStatusSerializer(many=True)}
    )
    def get(self, request):
        accounts = BufferAccount.objects.filter(
            user=request.user
        ).order_by('-created_at')
        serializer = BufferAccountStatusSerializer(accounts, many=True)
        return Response(serializer.data)


class BufferAccountUpdateView(BufferAPIView):
    """Update a Buffer account's name or api_url."""

    @extend_schema(
        summary="Update Buffer account",
        description="Update a Buffer account's display name or API URL.",
        request=BufferAccountStatusSerializer,
        responses={200: BufferAccountStatusSerializer}
    )
    def patch(self, request, pk=None):
        try:
            account = BufferAccount.objects.get(id=pk, user=request.user)
        except BufferAccount.DoesNotExist:
            return Response(
                {'error': 'Account not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if 'name' in request.data:
            account.name = request.data['name']
        if 'api_url' in request.data:
            account.api_url = request.data['api_url']
        account.save()

        return Response(BufferAccountStatusSerializer(account).data)


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

    def get_permissions(self):
        if self.action in ('fetch_recent', 'post_all'):
            return [permissions.AllowAny()]
        return super().get_permissions()
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return NewsDetailSerializer
        return NewsSerializer
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Filter by buffer account
        buffer_account_id = self.request.query_params.get('buffer_account')
        if buffer_account_id:
            queryset = queryset.filter(buffer_account_id=buffer_account_id)
        
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
        """Trigger manual news fetch and post synchronously"""
        result = fetch_and_queue_news()
        return Response({
            'message': 'News fetch completed',
            'result': result,
        })

    @action(detail=False, methods=['post'])
    def fetch_recent(self, request):
        """
        Fetch news from each connected BufferAccount's API URL synchronously,
        deduplicate against existing News rows per account, and queue
        matching items for posting to that account's Buffer.

        Returns a summary of what was fetched, saved, and queued per account.
        """
        try:
            with task_lock('fetch_recent'):
                return self._fetch_recent_inner(request)
        except AlreadyRunning as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

    def _fetch_recent_inner(self, request):
        accounts = list(BufferAccount.objects.filter(
            connection_status='connected'
        ).order_by('-updated_at'))

        if not accounts:
            return Response(
                {"error": "No connected Buffer accounts found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Pre-fetch Buffer platform once — avoids get_or_create per item.
        buffer_platform, _ = SocialPlatform.objects.get_or_create(
            name='buffer',
            defaults={'enabled': False},
        )

        # Pre-fetch enabled filter rules once — avoids re-querying per news item.
        from news.models import NewsFilterRule
        filter_rules = list(NewsFilterRule.objects.filter(enabled=True))

        accounts_processed = 0
        total_fetched = 0
        total_new = 0
        total_duplicates = 0
        total_queued = 0
        total_pages = 0
        account_results = []

        for account in accounts:
            fetcher = NewsFetcher(api_url=account.api_url)
            url = fetcher.api_url

            # Bulk-load existing api_news_ids for this account to avoid
            # per-item EXISTS queries (N+1 elimination).
            existing_ids = set(
                News.objects.filter(
                    buffer_account=account
                ).values_list('api_news_id', flat=True)
            )

            acct_fetched = 0
            acct_new = 0
            acct_duplicates = 0
            acct_queued = 0
            acct_pages = 0
            max_pages = 10

            while url and acct_pages < max_pages:
                # Release DB connection before upstream HTTP call.
                close_old_connections()

                try:
                    response = fetcher.session.get(url, timeout=30)
                    response.raise_for_status()
                    data = response.json()
                except requests.exceptions.Timeout:
                    account_results.append({
                        'account_id': account.id,
                        'account_name': account.name,
                        'error': 'Upstream API timed out after 30s',
                    })
                    break
                except requests.exceptions.RequestException as exc:
                    account_results.append({
                        'account_id': account.id,
                        'account_name': account.name,
                        'error': f'Upstream API request failed: {exc}',
                    })
                    break
                except ValueError:
                    account_results.append({
                        'account_id': account.id,
                        'account_name': account.name,
                        'error': 'Upstream API returned invalid JSON',
                    })
                    break

                acct_pages += 1
                results = data.get("results", [])
                acct_fetched += len(results)

                if not results:
                    break

                for item in results:
                    api_id = item.get("id")
                    if not api_id:
                        continue

                    if api_id in existing_ids:
                        acct_duplicates += 1
                        continue

                    news = News.create_from_api(item, buffer_account=account)
                    existing_ids.add(api_id)
                    acct_new += 1

                    if _should_post_news(news, filter_rules):
                        PostJob.objects.create(
                            news=news,
                            platform=buffer_platform,
                            buffer_account=account,
                            status="pending",
                        )
                        acct_queued += 1

                url = data.get("next")

            account_results.append({
                'account_id': account.id,
                'account_name': account.name,
                'fetched': acct_fetched,
                'new': acct_new,
                'duplicates': acct_duplicates,
                'queued': acct_queued,
                'pages_fetched': acct_pages,
            })

            total_fetched += acct_fetched
            total_new += acct_new
            total_duplicates += acct_duplicates
            total_queued += acct_queued
            total_pages += acct_pages
            accounts_processed += 1

        return Response({
            "accounts_processed": accounts_processed,
            "fetched": total_fetched,
            "new": total_new,
            "duplicates": total_duplicates,
            "queued": total_queued,
            "pages_fetched": total_pages,
            "accounts": account_results,
        })

    
    import concurrent.futures

    @action(detail=False, methods=["post"])
    def post_all(self, request):
        """
        Find all news that have never been successfully posted and that
        don't already have pending/processing jobs, and post up to
        `batch_size` of them immediately (no Celery), processing jobs
        concurrently to reduce wall-clock time.

        Only considers news fetched within the last 85 minutes.
        Optionally filter by buffer_account_id.
        """
        try:
            with task_lock('post_all'):
                return self._post_all_inner(request)
        except AlreadyRunning as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )

    def _post_all_inner(self, request):
        try:
            batch_size = int(request.data.get("batch_size") or request.query_params.get("batch_size") or 5)
        except (TypeError, ValueError):
            batch_size = 5
        batch_size = max(1, min(batch_size, 20))

        try:
            max_workers = int(request.data.get("max_workers") or request.query_params.get("max_workers") or 5)
        except (TypeError, ValueError):
            max_workers = 5
        max_workers = max(1, min(max_workers, 10))

        # Optional account filter
        buffer_account_id = request.data.get("buffer_account_id") or request.query_params.get("buffer_account_id")

        cutoff = timezone.now() - timedelta(minutes=90)

        successful_ids = PostJob.objects.filter(status="success").values("news_id")

        base_qs = (
            News.objects.filter(fetched_at__gte=cutoff)
            .exclude(id__in=successful_ids)
            .order_by("-published")
        )

        if buffer_account_id:
            base_qs = base_qs.filter(buffer_account_id=buffer_account_id)

        # Collect candidate news IDs first (bounded by batch_size).
        candidate_ids = list(base_qs.values_list('id', flat=True)[:batch_size])
        total = len(candidate_ids)

        if not candidate_ids:
            return Response({
                "total": 0,
                "batch_size": batch_size,
                "max_workers": max_workers,
                "jobs_created": 0,
                "skipped_no_account": 0,
                "posted": 0,
                "failed": 0,
                "remaining": 0,
                "errors": [],
                "db_pool": connection_snapshot(),
            })

        buffer_platform, _ = SocialPlatform.objects.get_or_create(
            name='buffer', defaults={'enabled': False}
        )

        # Pre-load a fallback connected account for news items without one
        fallback_account = BufferAccount.objects.filter(
            connection_status='connected'
        ).order_by('-updated_at').first()

        # Batch-load existing jobs for all candidate news — eliminates
        # per-item PostJob.objects.filter().first() (N+1 elimination).
        existing_jobs_map = {}
        for job in PostJob.objects.filter(
            news_id__in=candidate_ids,
            status__in=["pending", "failed", "permanent_fail"],
        ).select_related('news'):
            # Keep only the most recent job per news item.
            if job.news_id not in existing_jobs_map:
                existing_jobs_map[job.news_id] = job

        skipped_no_account = 0
        jobs_created = 0
        jobs_to_process = []

        # Phase 1: pick news items and prepare jobs
        # Re-fetch full News objects only for the candidates.
        news_by_id = {
            n.id: n for n in News.objects.filter(id__in=candidate_ids)
        }

        for news_id in candidate_ids:
            if jobs_created >= batch_size:
                break

            news = news_by_id[news_id]

            existing_job = existing_jobs_map.get(news_id)
            if existing_job:
                if existing_job.status != "pending":
                    existing_job.status = "pending"
                    existing_job.retry_count = 0
                    existing_job.last_error = None
                    existing_job.next_retry_at = None
                    existing_job.save(update_fields=[
                        "status", "retry_count", "last_error", "next_retry_at",
                    ])
                jobs_to_process.append((existing_job.id, news_id))
                jobs_created += 1
                continue

            acct = news.buffer_account
            if not acct or acct.connection_status != 'connected':
                acct = fallback_account
            if not acct:
                skipped_no_account += 1
                continue

            job = PostJob.objects.create(
                news=news,
                platform=buffer_platform,
                buffer_account=acct,
                status="pending",
            )
            jobs_to_process.append((job.id, news_id))
            jobs_created += 1

        # Release main thread DB connection before concurrent processing.
        # Each worker thread manages its own connection via process_post_job().
        close_old_connections()

        # Phase 2: process all created jobs concurrently
        posted_count = 0
        failed_count = 0
        errors = []

        if jobs_to_process:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_ids = {
                    executor.submit(monitor_post_job, job_id): (job_id, news_id)
                    for job_id, news_id in jobs_to_process
                }
                for future in concurrent.futures.as_completed(future_to_ids):
                    job_id, news_id = future_to_ids[future]
                    try:
                        result = future.result()
                        if result.get("status") == "success":
                            posted_count += 1
                        else:
                            failed_count += 1
                            errors.append({
                                "news_id": news_id,
                                "error": result.get("error", result.get("status", "unknown")),
                            })
                    except Exception as exc:
                        failed_count += 1
                        errors.append({
                            "news_id": news_id,
                            "error": str(exc),
                        })
            close_old_connections()

        remaining = max(total - skipped_no_account - jobs_created, 0)

        return Response({
            "total": total,
            "batch_size": batch_size,
            "max_workers": max_workers,
            "jobs_created": jobs_created,
            "skipped_no_account": skipped_no_account,
            "posted": posted_count,
            "failed": failed_count,
            "remaining": remaining,
            "errors": errors if errors else [],
            "db_pool": connection_snapshot(),
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
    permission_classes = [permissions.AllowAny]
    
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
        result = process_post_job(job.id)
        
        return Response({
            'message': 'Job processed',
            'job_id': job.id,
            'result': result,
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
        
        result = process_post_job(job.id)
        
        return Response({
            'message': 'Job retried',
            'job_id': job.id,
            'result': result,
        })
    
    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """Get logs for a job"""
        job = self.get_object()
        logs = job.logs.all().order_by('-created_at')
        return Response(PostLogSerializer(logs, many=True).data)

    @action(detail=False, methods=['post'])
    def cleanup(self, request):
        """Delete all PostJob records older than 3 days."""
        cutoff = timezone.now() - timedelta(days=3)
        deleted_count, _ = PostJob.objects.filter(created_at__lt=cutoff).delete()
        return Response({
            'message': f'Deleted {deleted_count} old post jobs',
            'deleted': deleted_count,
        })


# ============ STATS VIEWS ============

class DashboardStatsView(APIView):
    """Get dashboard statistics"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        today = timezone.now().date()
        today_start = timezone.make_aware(
            datetime.combine(today, datetime.min.time())
        )
        
        # News stats — single query each.
        total_news = News.objects.count()
        news_today = News.objects.filter(fetched_at__gte=today_start).count()
        
        # Post stats — single query with aggregation instead of 5 separate counts.
        from django.db.models import Count, Q as QFilter
        post_stats = PostJob.objects.aggregate(
            total=Count('id'),
            today_count=Count('id', filter=QFilter(created_at__gte=today_start)),
            pending=Count('id', filter=QFilter(status='pending')),
            failed=Count('id', filter=QFilter(status__in=['failed', 'permanent_fail'])),
            success=Count('id', filter=QFilter(status='success')),
        )
        
        total_posts = post_stats['total']
        success_rate = (post_stats['success'] / total_posts * 100) if total_posts > 0 else 0
        
        # Platform stats — single aggregated query instead of N+1.
        platform_rows = (
            PostJob.objects
            .values('platform__name')
            .annotate(
                total=Count('id'),
                success=Count('id', filter=QFilter(status='success')),
                failed=Count('id', filter=QFilter(status__in=['failed', 'permanent_fail'])),
                pending=Count('id', filter=QFilter(status='pending')),
            )
            .order_by('platform__name')
        )
        
        platforms = {}
        for row in platform_rows:
            name = row['platform__name']
            t = row['total']
            platforms[name] = {
                'total': t,
                'success': row['success'],
                'failed': row['failed'],
                'pending': row['pending'],
                'success_rate': round((row['success'] / t * 100) if t > 0 else 0, 2),
            }
        
        # Category stats — single annotated query instead of N+1.
        category_rows = (
            News.objects
            .filter(category__isnull=False)
            .exclude(category='')
            .values('category')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        
        categories = {row['category']: row['count'] for row in category_rows}
        
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
            'posts_today': post_stats['today_count'],
            'pending_posts': post_stats['pending'],
            'failed_posts': post_stats['failed'],
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
        from django.db.models import Count, Q as QFilter, Max
        
        rows = (
            PostJob.objects
            .values('platform__name')
            .annotate(
                total=Count('id'),
                success=Count('id', filter=QFilter(status='success')),
                failed=Count('id', filter=QFilter(status__in=['failed', 'permanent_fail'])),
                pending=Count('id', filter=QFilter(status='pending')),
                last_posted_at=Max('posted_at', filter=QFilter(status='success')),
            )
            .order_by('platform__name')
        )
        
        stats = []
        for row in rows:
            t = row['total']
            stats.append({
                'platform': row['platform__name'],
                'total_posts': t,
                'success_posts': row['success'],
                'failed_posts': row['failed'],
                'pending': row['pending'],
                'success_rate': round((row['success'] / t * 100) if t > 0 else 0, 2),
                'last_post': row['last_posted_at'],
            })
        
        return Response(stats)


# ============ SETTINGS VIEWS ============

class SystemSettingsView(APIView):
    """Manage system settings"""
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        """Get current settings"""
        settings_data = {
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
        return Response({
            'message': 'Settings updated successfully',
            'settings': request.data
        })





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