from unittest.mock import patch, MagicMock
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from news.models import News, NewsFilterRule
from posts.models import PostJob, ConcurrentTaskLock
from social.models import SocialPlatform, BufferAccount

User = get_user_model()


class FetchRecentEndpointTest(TestCase):
    """Tests for ``POST /api/news/fetch_recent/``."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="fetchuser", password="testpass"
        )

        cls.platform_buffer, _ = SocialPlatform.objects.get_or_create(
            name="buffer", defaults={"enabled": True}
        )
        cls.platform_buffer.enabled = True
        cls.platform_buffer.save(update_fields=["enabled"])

        cls.buffer_account = BufferAccount.objects.create(
            user=cls.user,
            name="Test Account",
            api_key="test-key",
            api_url="https://example.com/api/news/",
            connection_status="connected",
        )

    def setUp(self):
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _now_iso(self, offset_hours=0):
        """Return an ISO-format timestamp at *offset_hours* offset from now."""
        dt = timezone.now() + timedelta(hours=offset_hours)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _make_api_item(self, item_id, hours_ago=0):
        return {
            "id": item_id,
            "title": f"Article {item_id}",
            "summary": f"Summary of article {item_id}.",
            "link": f"https://example.com/{item_id}",
            "image": f"https://example.com/{item_id}.jpg",
            "published": self._now_iso(-hours_ago),
            "category": "tech",
            "country": "Nigeria",
            "source": "TestSource",
        }

    def _mock_session_get(self, items_by_page):
        """Return a side-effect for ``session.get``.

        ``items_by_page``: list of (items_list, has_next) tuples.
        """
        def side_effect(url, **kwargs):
            idx = getattr(side_effect, "call_count", 0)
            setattr(side_effect, "call_count", idx + 1)
            mock_resp = MagicMock(status_code=200)
            if idx >= len(items_by_page):
                mock_resp.json.return_value = {"results": [], "next": None}
            else:
                items, has_next = items_by_page[idx]
                mock_resp.json.return_value = {
                    "results": items,
                    "next": "http://example.com/?p=2" if has_next else None,
                }
            return mock_resp

        side_effect.call_count = 0
        return side_effect

    def _patch_fetcher(self, items_by_page, should_post=True):
        """Patch ``NewsFetcher`` and return the mock instance."""
        patcher = patch("api.views.NewsFetcher")
        mock_cls = patcher.start()
        self.addCleanup(patcher.stop)

        mock_fetcher = MagicMock()
        mock_cls.return_value = mock_fetcher
        mock_fetcher.api_url = self.buffer_account.api_url
        mock_fetcher.session.get.side_effect = self._mock_session_get(items_by_page)
        mock_fetcher.should_post_news.return_value = should_post
        return mock_fetcher

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_happy_path_new_items_queued(self):
        self._patch_fetcher([([self._make_api_item(1), self._make_api_item(2)], False)])

        response = self.api_client.post("/api/news/fetch_recent/", format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["fetched"], 2)
        self.assertEqual(data["new"], 2)
        self.assertEqual(data["duplicates"], 0)
        self.assertGreater(data["queued"], 0)

    def test_duplicates_skipped(self):
        News.objects.create(
            api_news_id=99,
            title="Existing",
            link="https://example.com/99",
            published=timezone.now(),
            category="tech",
            source="Test",
            buffer_account=self.buffer_account,
        )
        self._patch_fetcher([([self._make_api_item(99), self._make_api_item(100)], False)])

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["fetched"], 2)
        self.assertEqual(data["new"], 1)
        self.assertEqual(data["duplicates"], 1)

    def test_upstream_timeout(self):
        from requests.exceptions import Timeout

        mock_fetcher = MagicMock()
        mock_fetcher.api_url = self.buffer_account.api_url
        mock_fetcher.session.get.side_effect = Timeout("Connection timed out")

        patcher = patch("api.views.NewsFetcher", return_value=mock_fetcher)
        patcher.start()
        self.addCleanup(patcher.stop)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("accounts", data)
        self.assertTrue(any("error" in a for a in data["accounts"]))

    def test_upstream_bad_json(self):
        mock_fetcher = MagicMock()
        mock_fetcher.api_url = self.buffer_account.api_url
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("No JSON")
        mock_fetcher.session.get.return_value = mock_resp

        patcher = patch("api.views.NewsFetcher", return_value=mock_fetcher)
        patcher.start()
        self.addCleanup(patcher.stop)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(any("error" in a for a in data["accounts"]))

    def test_max_pages_hard_cap(self):
        pages = [([self._make_api_item(400 + i)], True) for i in range(11)]
        self._patch_fetcher(pages)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["pages_fetched"], 10)
        self.assertEqual(data["fetched"], 10)

    def test_no_connected_accounts(self):
        self.buffer_account.connection_status = "disconnected"
        self.buffer_account.save(update_fields=["connection_status"])

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PostAllEndpointTest(TestCase):
    """Tests for ``POST /api/news/post_all/``."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="postalluser", password="testpass"
        )

        cls.platform_buffer, _ = SocialPlatform.objects.get_or_create(
            name="buffer", defaults={"enabled": True}
        )
        cls.platform_buffer.enabled = True
        cls.platform_buffer.save(update_fields=["enabled"])

        cls.buffer_account = BufferAccount.objects.create(
            user=cls.user,
            name="Test Account",
            api_key="test-key",
            api_url="https://example.com/api/news/",
            connection_status="connected",
        )

    def setUp(self):
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_news(self, api_news_id, **kwargs):
        defaults = dict(
            title=f"Article {api_news_id}",
            link=f"https://example.com/{api_news_id}",
            published=timezone.now() - timedelta(minutes=30),
            category="tech",
            country="Nigeria",
            source="TestSource",
            buffer_account=self.buffer_account,
        )
        defaults.update(kwargs)
        return News.objects.create(api_news_id=api_news_id, **defaults)

    def _patch_process_post_job(self, status="success"):
        """Patch monitor_post_job to return immediately instead of calling real services."""
        patcher = patch("api.views.monitor_post_job")
        mock_fn = patcher.start()
        self.addCleanup(patcher.stop)
        mock_fn.return_value = {"status": status, "job_id": 1}
        return mock_fn

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_posts_unposted_news(self):
        """News with no PostJobs at all should be posted."""
        self._make_news(1)
        self._make_news(2)
        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["posted"], 2)

    def test_skips_already_successful_news(self):
        """News with a successful PostJob should be excluded."""
        news = self._make_news(10)
        PostJob.objects.create(
            news=news,
            platform=self.platform_buffer,
            buffer_account=self.buffer_account,
            status="success",
        )

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 0)

    def test_processes_pending_jobs(self):
        """News with a pending PostJob should be reprocessed (no Celery worker)."""
        news = self._make_news(20)
        PostJob.objects.create(
            news=news,
            platform=self.platform_buffer,
            buffer_account=self.buffer_account,
            status="pending",
        )

        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["posted"], 1)

    def test_posts_partially_failed_news(self):
        """News where all jobs are failed/permanent_fail/skipped should be posted again."""
        news = self._make_news(30)
        PostJob.objects.create(
            news=news,
            platform=self.platform_buffer,
            buffer_account=self.buffer_account,
            status="failed",
        )

        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertGreater(data["posted"], 0)

    def test_no_platforms_posts_nothing(self):
        """With no enabled buffer accounts, posted should be 0."""
        self.buffer_account.connection_status = "disconnected"
        self.buffer_account.save(update_fields=["connection_status"])

        self._make_news(60)
        self._make_news(61)
        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["posted"], 0)

    def test_reports_failed_posts(self):
        """When process_post_job returns failure, failed count should increment."""
        self._make_news(70)
        self._patch_process_post_job("failed")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["posted"], 0)
        self.assertEqual(data["failed"], 1)

    def test_allowany_works(self):
        """post_all uses AllowAny, so unauthenticated requests should work."""
        client = APIClient()
        response = client.post("/api/news/post_all/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filters_by_account(self):
        """When buffer_account_id is provided, only that account's news is processed."""
        other_account = BufferAccount.objects.create(
            user=self.user,
            name="Other Account",
            api_key="other-key",
            api_url="https://other.example.com/api/",
            connection_status="connected",
        )
        self._make_news(80, buffer_account=self.buffer_account)
        self._make_news(81, buffer_account=other_account)
        self._patch_process_post_job("success")

        response = self.api_client.post(
            "/api/news/post_all/",
            {"buffer_account_id": self.buffer_account.id},
            format="json",
        )
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["posted"], 1)


class FetchRecentLockTest(TestCase):
    """Tests that fetch_recent returns 409 when already running."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="lockuser", password="testpass"
        )
        cls.platform_buffer, _ = SocialPlatform.objects.get_or_create(
            name="buffer", defaults={"enabled": True}
        )
        cls.platform_buffer.enabled = True
        cls.platform_buffer.save(update_fields=["enabled"])
        cls.buffer_account = BufferAccount.objects.create(
            user=cls.user,
            name="Test Account",
            api_key="test-key",
            api_url="https://example.com/api/news/",
            connection_status="connected",
        )

    def setUp(self):
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)

    def test_returns_409_when_already_running(self):
        """A second fetch_recent call while one is running should return 409."""
        ConcurrentTaskLock.objects.create(
            task_name="fetch_recent",
            locked_at=timezone.now(),
        )
        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("error", response.json())

    def test_proceeds_when_lock_is_stale(self):
        """A stale lock (older than 20 min) should be overridden."""
        ConcurrentTaskLock.objects.create(
            task_name="fetch_recent",
            locked_at=timezone.now() - timezone.timedelta(seconds=1200),
        )
        mock_fetcher = MagicMock()
        mock_fetcher.api_url = self.buffer_account.api_url
        mock_fetcher.session.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"results": [], "next": None},
        )
        with patch("api.views.NewsFetcher", return_value=mock_fetcher):
            response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class PostAllLockTest(TestCase):
    """Tests that post_all returns 409 when already running."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="lockuser2", password="testpass"
        )
        cls.platform_buffer, _ = SocialPlatform.objects.get_or_create(
            name="buffer", defaults={"enabled": True}
        )
        cls.platform_buffer.enabled = True
        cls.platform_buffer.save(update_fields=["enabled"])
        cls.buffer_account = BufferAccount.objects.create(
            user=cls.user,
            name="Test Account",
            api_key="test-key",
            api_url="https://example.com/api/news/",
            connection_status="connected",
        )

    def setUp(self):
        self.api_client = APIClient()
        self.api_client.force_authenticate(user=self.user)

    def test_returns_409_when_already_running(self):
        """A second post_all call while one is running should return 409."""
        ConcurrentTaskLock.objects.create(
            task_name="post_all",
            locked_at=timezone.now(),
        )
        response = self.api_client.post("/api/news/post_all/", format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("error", response.json())

    def test_proceeds_when_lock_is_stale(self):
        """A stale lock should be overridden and the operation should proceed."""
        ConcurrentTaskLock.objects.create(
            task_name="post_all",
            locked_at=timezone.now() - timezone.timedelta(seconds=1200),
        )
        response = self.api_client.post("/api/news/post_all/", format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)