# news/tasks.py
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from .services import NewsFetcher
from .models import News
from posts.models import PostJob
from social.models import BufferAccount, SocialPlatform, TelegramChannel
from posts.tasks import process_post_job

@shared_task
def fetch_and_queue_news():
    """Main task: Fetch news and queue for posting"""
    
    print(f"Starting news fetch at {timezone.now()}")
    
    # Fetch news
    fetcher = NewsFetcher()
    news_list = fetcher.fetch_news(time_frame='today')
    
    if not news_list:
        print("No news fetched")
        return {"status": "no_news", "count": 0}
    
    # Process and save
    saved_count, new_news = fetcher.process_news(news_list)
    print(f"Saved {saved_count} new news articles")
    
    # Filter and queue for posting
    queued_count = 0
    for news in new_news:
        # Check if should post
        if not fetcher.should_post_news(news):
            continue
        
        # Get enabled supported platforms
        platforms = SocialPlatform.objects.filter(
            enabled=True,
            name__in=['telegram', 'buffer']
        )

        # Create post jobs
        for platform in platforms:
            # For Telegram, we need to specify channels
            if platform.name == 'telegram':
                channels = TelegramChannel.objects.filter(enabled=True, is_verified=True)
                for channel in channels:
                    PostJob.objects.create(
                        news=news,
                        platform=platform,
                        telegram_channel=channel,
                        status='pending'
                    )
                    queued_count += 1
            elif platform.name == 'buffer':
                buffer_accounts = BufferAccount.objects.filter(
                    connection_status='connected'
                ).order_by('-updated_at')
                for buffer_account in buffer_accounts:
                    PostJob.objects.create(
                        news=news,
                        platform=platform,
                        buffer_account=buffer_account,
                        status='pending'
                    )
                    queued_count += 1
    
    print(f"Queued {queued_count} post jobs")
    
    return {
        "status": "success",
        "news_saved": saved_count,
        "jobs_queued": queued_count
    }


@shared_task
def fetch_news_test():
    """Test task for fetching news"""
    fetcher = NewsFetcher()
    news_list = fetcher.fetch_news(time_frame='today', max_pages=1)
    return {"count": len(news_list)}


@shared_task
def process_telegram_posts():
    """Process pending Telegram posts"""
    from posts.models import PostJob
    
    # Get pending Telegram jobs
    telegram_platform = SocialPlatform.objects.filter(name='telegram', enabled=True).first()
    if not telegram_platform:
        return {"status": "telegram_not_enabled"}
    
    pending_jobs = PostJob.objects.filter(
        platform=telegram_platform,
        status='pending',
        telegram_channel__isnull=False,
        telegram_channel__enabled=True
    ).select_related('news', 'telegram_channel')
    
    processed = 0
    for job in pending_jobs:
        try:
            process_post_job.delay(job.id)
            processed += 1
        except Exception as e:
            print(f"Error processing job {job.id}: {e}")
    
    return {"processed": processed}
