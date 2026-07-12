# news/tasks.py
from django.utils import timezone
from django.db import transaction
from .services import NewsFetcher
from .models import News
from posts.models import PostJob
from social.models import BufferAccount, SocialPlatform
from posts.tasks import process_post_job


def fetch_and_queue_news():
    """Fetch news from all connected BufferAccounts and post synchronously."""

    print(f"Starting news fetch at {timezone.now()}")

    accounts = BufferAccount.objects.filter(
        connection_status='connected'
    ).order_by('-updated_at')

    if not accounts.exists():
        print("No connected Buffer accounts found")
        return {"status": "no_accounts", "count": 0}

    # Pre-fetch Buffer platform once — avoids get_or_create per news item.
    platform, _ = SocialPlatform.objects.get_or_create(
        name='buffer',
        defaults={'enabled': False}
    )

    total_saved = 0
    total_posted = 0

    for account in accounts:
        print(f"Fetching news for account: {account.name or account.id}")

        fetcher = NewsFetcher(api_url=account.api_url)
        news_list = fetcher.fetch_news(time_frame='today')

        if not news_list:
            print(f"No news fetched for account {account.name or account.id}")
            continue

        saved_count, new_news = fetcher.process_news(
            news_list,
            buffer_account=account
        )
        print(f"Saved {saved_count} new news articles for account {account.name or account.id}")
        total_saved += saved_count

        posted_count = 0
        for news in new_news:
            if not fetcher.should_post_news(news):
                continue

            job = PostJob.objects.create(
                news=news,
                platform=platform,
                buffer_account=account,
                status='pending'
            )
            result = process_post_job(job.id)
            if result.get('status') == 'success':
                posted_count += 1

        print(f"Posted {posted_count} articles for account {account.name or account.id}")
        total_posted += posted_count

    print(f"Total: saved {total_saved} news, posted {total_posted}")

    return {
        "status": "success",
        "news_saved": total_saved,
        "posted": total_posted,
    }
