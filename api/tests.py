from unittest.mock import patch, MagicMock
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient

from news.models import News, NewsFilterRule
from posts.models import PostJob
from social.models import SocialPlatform, TelegramChannel

User = get_user_model()


class FetchRecentEndpointTest(TestCase):
    """Tests for ``POST /api/news/fetch-recent/``."""

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

        cls.platform_telegram, _ = SocialPlatform.objects.get_or_create(
            name="telegram", defaults={"enabled": True}
        )
        cls.platform_telegram.enabled = True
        cls.platform_telegram.save(update_fields=["enabled"])
        cls.telegram_channel = TelegramChannel.objects.create(
            name="Test Channel",
            channel_username="@testchannel",
            channel_chat_id="-1001234567890",
            bot_token="123:abc",
            enabled=True,
            is_verified=True,
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
        mock_fetcher.API_URL = "https://ubuntureport.onrender.com/api/news/top-sources-recent/"
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
        self.assertEqual(data["outside_window"], 0)
        self.assertGreater(data["queued"], 0)

    def test_duplicates_skipped(self):
        News.objects.create(
            api_news_id=99,
            title="Existing",
            link="https://example.com/99",
            published=timezone.now(),
            category="tech",
            source="Test",
        )
        self._patch_fetcher([([self._make_api_item(99), self._make_api_item(100)], False)])

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["fetched"], 2)
        self.assertEqual(data["new"], 1)
        self.assertEqual(data["duplicates"], 1)

    def test_outside_window_skipped(self):
        self._patch_fetcher([([self._make_api_item(200, hours_ago=3)], False)])

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["fetched"], 1)
        self.assertEqual(data["new"], 0)
        self.assertEqual(data["outside_window"], 1)

    def test_early_pagination_stop(self):
        self._patch_fetcher(
            [
                ([self._make_api_item(300)], True),
                ([self._make_api_item(301, hours_ago=3)], False),
            ]
        )

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["fetched"], 2)
        self.assertEqual(data["new"], 1)
        self.assertEqual(data["outside_window"], 1)
        self.assertTrue(data["early_stopped"])

    def test_upstream_timeout(self):
        from requests.exceptions import Timeout

        mock_fetcher = MagicMock()
        mock_fetcher.API_URL = "https://ubuntureport.onrender.com/api/news/top-sources-recent/"
        mock_fetcher.session.get.side_effect = Timeout("Connection timed out")

        patcher = patch("api.views.NewsFetcher", return_value=mock_fetcher)
        patcher.start()
        self.addCleanup(patcher.stop)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("timed out", response.json()["error"])

    def test_upstream_bad_json(self):
        mock_fetcher = MagicMock()
        mock_fetcher.API_URL = "https://ubuntureport.onrender.com/api/news/top-sources-recent/"
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("No JSON")
        mock_fetcher.session.get.return_value = mock_resp

        patcher = patch("api.views.NewsFetcher", return_value=mock_fetcher)
        patcher.start()
        self.addCleanup(patcher.stop)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("invalid json", response.json()["error"].lower())

    def test_max_pages_hard_cap(self):
        pages = [([self._make_api_item(400 + i)], True) for i in range(11)]
        self._patch_fetcher(pages)

        response = self.api_client.post("/api/news/fetch_recent/", format="json")
        data = response.json()
        self.assertEqual(data["pages_fetched"], 10)
        self.assertEqual(data["fetched"], 10)


class PostAllEndpointTest(TestCase):
    """Tests for ``POST /api/news/post-all/``."""

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

        cls.platform_telegram, _ = SocialPlatform.objects.get_or_create(
            name="telegram", defaults={"enabled": True}
        )
        cls.platform_telegram.enabled = True
        cls.platform_telegram.save(update_fields=["enabled"])
        cls.telegram_channel = TelegramChannel.objects.create(
            name="PostAll Channel",
            channel_username="@postalltest",
            channel_chat_id="-1009999999999",
            bot_token="999:abc",
            enabled=True,
            is_verified=True,
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
        )
        defaults.update(kwargs)
        return News.objects.create(api_news_id=api_news_id, **defaults)

    def _patch_process_post_job(self, status="success"):
        """Patch process_post_job to return immediately instead of calling real services."""
        patcher = patch("api.views.process_post_job")
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
        self.assertEqual(data["posted"], 2)  # 2 news × 1 telegram channel

    def test_skips_already_successful_news(self):
        """News with a successful PostJob should be excluded."""
        news = self._make_news(10)
        PostJob.objects.create(
            news=news,
            platform=self.platform_telegram,
            telegram_channel=self.telegram_channel,
            status="success",
        )

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 0)

    def test_skips_already_pending_news(self):
        """News with a pending/processing PostJob should be excluded."""
        news = self._make_news(20)
        PostJob.objects.create(
            news=news,
            platform=self.platform_telegram,
            telegram_channel=self.telegram_channel,
            status="pending",
        )

        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 0)

    def test_posts_partially_failed_news(self):
        """News where all jobs are failed/permanent_fail/skipped should be posted again."""
        news = self._make_news(30)
        PostJob.objects.create(
            news=news,
            platform=self.platform_telegram,
            telegram_channel=self.telegram_channel,
            status="failed",
        )
        PostJob.objects.create(
            news=news,
            platform=self.platform_buffer,
            status="failed",
        )

        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 1)
        self.assertGreater(data["posted"], 0)

    def test_respects_filter_rules(self):
        """News blocked by filter rules should be filtered_out."""
        NewsFilterRule.objects.create(
            rule_type="keyword",
            rule_action="exclude",
            value="Article",
            enabled=True,
        )
        self._make_news(40)
        self._make_news(41)

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["filtered_out"], 2)
        self.assertEqual(data["posted"], 0)

    def test_mixed_filter_and_post(self):
        """Some pass filter, some don't — counts should reflect that."""
        NewsFilterRule.objects.create(
            rule_type="category",
            rule_action="include",
            value="tech",
            enabled=True,
        )
        self._make_news(50, category="tech")
        self._make_news(51, category="sports")
        self._make_news(52, category="tech")
        self._patch_process_post_job("success")

        response = self.api_client.post("/api/news/post_all/", format="json")
        data = response.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["filtered_out"], 1)
        self.assertEqual(data["posted"], 2)  # 2 tech news × 1 telegram channel

    def test_no_platforms_posts_nothing(self):
        """With no enabled platforms, posted should be 0."""
        self.platform_buffer.enabled = False
        self.platform_buffer.save(update_fields=["enabled"])
        self.platform_telegram.enabled = False
        self.platform_telegram.save(update_fields=["enabled"])

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

    def test_authenticated_required(self):
        """Unauthenticated requests should get 401."""
        client = APIClient()
        response = client.post("/api/news/post_all/", format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
