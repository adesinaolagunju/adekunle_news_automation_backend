# news/tasks.py
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from .services import NewsFetcher
from .models import News
from posts.models import PostJob
from social.models import BufferAccount, SocialPlatform
from posts.tasks import process_post_job

@shared_task
def fetch_and_queue_news():
    """Main task: Fetch news from all connected BufferAccounts and queue for posting"""
    
    print(f"Starting news fetch at {timezone.now()}")
    
    accounts = BufferAccount.objects.filter(
        connection_status='connected'
    ).order_by('-updated_at')
    
    if not accounts.exists():
        print("No connected Buffer accounts found")
        return {"status": "no_accounts", "count": 0}
    
    total_saved = 0
    total_queued = 0
    
    for account in accounts:
        print(f"Fetching news for account: {account.name or account.id}")
        
        fetcher = NewsFetcher(api_url=account.api_url)
        news_list = fetcher.fetch_news(time_frame='today')
        
        if not news_list:
            print(f"No news fetched for account {account.name or account.id}")
            continue
        
        # Process and save with account association
        saved_count, new_news = fetcher.process_news(
            news_list,
            buffer_account=account
        )
        print(f"Saved {saved_count} new news articles for account {account.name or account.id}")
        total_saved += saved_count
        
        # Filter and queue for posting
        queued_count = 0
        for news in new_news:
            if not fetcher.should_post_news(news):
                continue
            
            platform, _ = SocialPlatform.objects.get_or_create(
                name='buffer',
                defaults={'enabled': False}
            )
            
            PostJob.objects.create(
                news=news,
                platform=platform,
                buffer_account=account,
                status='pending'
            )
            queued_count += 1
        
        print(f"Queued {queued_count} post jobs for account {account.name or account.id}")
        total_queued += queued_count
    
    print(f"Total: saved {total_saved} news, queued {total_queued} jobs")
    
    return {
        "status": "success",
        "news_saved": total_saved,
        "jobs_queued": total_queued
    }


@shared_task
def fetch_news_test():
    """Test task for fetching news"""
    fetcher = NewsFetcher()
    news_list = fetcher.fetch_news(time_frame='today', max_pages=1)
    return {"count": len(news_list)}
