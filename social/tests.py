from unittest.mock import patch, ANY

from django.contrib.auth import get_user_model
from django.test import TestCase

from news.models import News
from posts.models import PostJob
from posts.tasks import process_buffer_job
from social.buffer import BufferService, _clear_cache
from social.models import BufferAccount, SocialPlatform

User = get_user_model()


class PublishNowDirectTest(TestCase):
    """Tests for ``BufferService.publish_now()`` in isolation."""

    def test_publish_now_sends_shareNow_mode(self):
        service = BufferService(api_key="test_key")

        with patch.object(service, "_request") as mock_request:
            mock_request.return_value = {
                "createPost": {
                    "post": {"id": "p123", "text": "hello", "dueAt": None}
                },
            }

            result = service.publish_now(
                channel_id="ch1",
                text="Hello world",
            )

        self.assertEqual(result, {"id": "p123", "text": "hello", "dueAt": None})
        mock_request.assert_called_once()

        _call_args, call_kwargs = mock_request.call_args
        variables = call_kwargs.get("variables", {})
        post_input = variables.get("input", {})
        self.assertEqual(post_input.get("mode"), "shareNow")

    def test_publish_now_with_metadata_and_image(self):
        service = BufferService(api_key="test_key")

        with patch.object(service, "_request") as mock_request:
            mock_request.return_value = {
                "createPost": {
                    "post": {"id": "p456", "text": "hello", "dueAt": None}
                },
            }

            result = service.publish_now(
                channel_id="ch1",
                text="Hello with image",
                image_url="https://example.com/img.jpg",
                service="twitter",
            )

        self.assertEqual(result, {"id": "p456", "text": "hello", "dueAt": None})
        mock_request.assert_called_once()

        _call_args, call_kwargs = mock_request.call_args
        variables = call_kwargs.get("variables", {})
        post_input = variables.get("input", {})
        self.assertEqual(post_input.get("mode"), "shareNow")
        self.assertEqual(
            post_input.get("assets"),
            [{"image": {"url": "https://example.com/img.jpg"}}],
        )


class ProcessBufferJobTest(TestCase):
    """Tests for ``process_buffer_job()`` end-to-end through ``BufferService``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _clear_cache()

    def setUp(self):
        super().setUp()
        _clear_cache()

    @classmethod
    def setUpTestData(cls):
        user = User.objects.create_user(username="buffertest", password="testpass")
        cls.news = News.objects.create(
            api_news_id=999001,
            title="Test News Title",
            summary="Test summary.",
            link="https://example.com/news/1",
            published="2026-07-07T12:00:00Z",
            category="tech",
            source="TestSource",
        )
        cls.platform, _ = SocialPlatform.objects.get_or_create(
            name="buffer", defaults={"enabled": True}
        )
        cls.buffer_account = BufferAccount.objects.create(
            user=user,
            api_key="test_buffer_key",
            connection_status="connected",
        )
        cls.job = PostJob.objects.create(
            news=cls.news,
            platform=cls.platform,
            buffer_account=cls.buffer_account,
            status="pending",
        )

    @patch("posts.tasks.BufferService._request")
    def test_process_buffer_job_sends_shareNow_mode(self, mock_request):
        _set_side_effects(mock_request)

        result = process_buffer_job(self.job)

        self.assertIn("response", result)
        response = result["response"]
        self.assertEqual(response["channels_posted"], 1)
        self.assertEqual(response["channels_attempted"], 1)
        self.assertEqual(result["message_id"], "buf_post_1")

        _assert_last_call_has_mode(mock_request, "shareNow")

    def test_process_buffer_job_multiple_channels(self):
        with patch("posts.tasks.BufferService._request") as mock_request:
            mock_request.side_effect = [
                {"account": {"organizations": [{"id": "org1"}]}},
                {
                    "channels": [
                        {"id": "ch1", "service": "twitter", "displayName": "Twitter"},
                        {"id": "ch2", "service": "facebook", "displayName": "Facebook"},
                    ]
                },
                {"createPost": {"post": {"id": "buf_post_1", "text": "", "dueAt": None}}},
                {"createPost": {"post": {"id": "buf_post_2", "text": "", "dueAt": None}}},
            ]

            result = process_buffer_job(self.job)

        self.assertEqual(result["response"]["channels_posted"], 2)
        # Last mutation call should have shareNow
        _assert_last_call_has_mode(mock_request, "shareNow")

    def test_process_buffer_job_partial_failure(self):
        with patch("posts.tasks.BufferService._request") as mock_request:
            mock_request.side_effect = [
                {"account": {"organizations": [{"id": "org1"}]}},
                {
                    "channels": [
                        {"id": "ch1", "service": "twitter", "displayName": "Twitter"},
                        {"id": "ch2", "service": "facebook", "displayName": "Facebook"},
                    ]
                },
                {"createPost": {"post": {"id": "ok_1", "text": "", "dueAt": None}}},
                Exception("Facebook API error"),
            ]

            result = process_buffer_job(self.job)

        self.assertEqual(result["response"]["channels_posted"], 1)
        self.assertEqual(len(result["response"]["failures"]), 1)
        self.assertIn("Facebook", result["response"]["failures"][0]["error"])
        _assert_last_call_has_mode(mock_request, "shareNow")


def _set_side_effects(mock_request):
    """Helper to set a typical happy-path side_effect on _request."""
    mock_request.side_effect = [
        {"account": {"organizations": [{"id": "org1"}]}},
        {"channels": [{"id": "ch1", "service": "twitter", "displayName": "My Twitter"}]},
        {"createPost": {"post": {"id": "buf_post_1", "text": "", "dueAt": None}}},
    ]


def _assert_last_call_has_mode(mock_request, expected_mode):
    """Assert that the last ``_request`` call's variables contain ``mode`` matching *expected_mode*."""
    last_call = mock_request.call_args_list[-1]
    _call_args, call_kwargs = last_call
    variables = call_kwargs.get("variables", {})
    post_input = variables.get("input", {})
    assert post_input.get("mode") == expected_mode, (
        f"Expected mode={expected_mode!r}, got {post_input.get('mode')!r}"
    )
