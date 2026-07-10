from unittest.mock import patch, MagicMock

from django.test import TestCase

from posts.tasks import _build_hashtags, _build_buffer_message


def _mock_news(**kwargs):
    """Create a lightweight mock News object for testing.
    
    Only supplies the attributes that _build_hashtags and
    _build_buffer_message access — no DB needed.
    """
    fields = {
        "title": "Test Article Title",
        "summary": "A concise summary of the test article.",
        "link": "https://example.com/article/1",
        "category": "Politics",
        "country": "Nigeria",
        "source": "PunchNG",
    }
    fields.update(kwargs)
    return MagicMock(**fields)


class BuildHashtagsTest(TestCase):
    """Tests for ``_build_hashtags()``."""

    def test_category_country_and_source(self):
        news = _mock_news(category="Politics", country="Nigeria", source="PunchNG")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #Politics #Nigeria #PunchNG")

    def test_country_and_source(self):
        news = _mock_news(category="", country="Nigeria", source="PunchNG")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #Nigeria #PunchNG")

    def test_category_only(self):
        news = _mock_news(category="Sports", country="", source="")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #Sports")

    def test_country_only(self):
        news = _mock_news(category="", country="Ghana", source="")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #Ghana")

    def test_source_only(self):
        news = _mock_news(category="", country="", source="BBC News")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #BBCNews")

    def test_neither_country_nor_source(self):
        news = _mock_news(category="", country="", source="")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest")

    def test_multiple_word_category_strips_spaces(self):
        news = _mock_news(category="Entertainment", country="United States", source="TechCrunch")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest #Entertainment #UnitedStates #TechCrunch")

    def test_special_characters_stripped(self):
        news = _mock_news(category="Sci-Tech", country="Côte d'Ivoire", source="O'Reilly Media")
        self.assertEqual(
            _build_hashtags(news),
            "#BreakingNews #Latest #SciTech #CtedIvoire #OReillyMedia",
        )

    def test_punctuation_only_value(self):
        """A category/country/source consisting entirely of punctuation should be skipped."""
        news = _mock_news(category="!!!", country="!!!", source="???")
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest")

    def test_none_category_country_or_source(self):
        news = _mock_news(category=None, country=None, source=None)
        self.assertEqual(_build_hashtags(news), "#BreakingNews #Latest")


class BuildBufferMessageTest(TestCase):
    """Tests for ``_build_buffer_message()``."""

    def test_full_message_with_summary(self):
        news = _mock_news(
            title="Test Title",
            summary="Test summary.",
            link="https://example.com",
            category="Politics",
            country="Nigeria",
            source="PunchNG",
        )
        result = _build_buffer_message(news)
        expected = (
            "📰 Test Title\n"
            "\n"
            "Test summary.\n"
            "\n"
            "Read More👇\n"
            "https://example.com\n"
            "\n"
            "\n"
            "#BreakingNews #Latest #Politics #Nigeria #PunchNG"
        )
        self.assertEqual(result, expected)

    def test_message_without_summary(self):
        news = _mock_news(
            title="Test Title",
            summary="",
            link="https://example.com",
            category="Sports",
            country="Ghana",
            source="BBC",
        )
        result = _build_buffer_message(news)
        self.assertIn("📰 Test Title", result)
        self.assertNotIn("Test summary", result)
        self.assertIn("Read More👇", result)
        self.assertIn("https://example.com", result)
        self.assertIn("#BreakingNews #Latest #Sports #Ghana #BBC", result)

    def test_no_category_country_or_source_hashtags(self):
        news = _mock_news(
            title="Test Title",
            summary="Summary.",
            link="https://example.com",
            category="",
            country="",
            source="",
        )
        result = _build_buffer_message(news)
        self.assertIn("#BreakingNews #Latest", result)
        self.assertNotIn("#None", result)
        self.assertNotIn("# ", result)
