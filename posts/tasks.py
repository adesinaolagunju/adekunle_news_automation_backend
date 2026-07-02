# posts/tasks.py
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from .models import PostJob, PostLog
from social.telegram import TelegramService, TelegramPostFormatter
from social.models import TelegramChannel

@shared_task
def process_post_job(job_id):
    """Process a single post job"""
    
    try:
        job = PostJob.objects.select_related('news', 'platform').get(id=job_id)
    except PostJob.DoesNotExist:
        return {"status": "job_not_found"}
    
    # Check if job should be processed
    if job.status == 'success':
        return {"status": "already_success"}
    
    if job.status == 'permanent_fail':
        return {"status": "permanent_fail"}
    
    # Log start
    PostLog.objects.create(
        job=job,
        action='start_processing',
        status=job.status,
        message='Started processing job'
    )
    
    try:
        # Process based on platform
        if job.platform.name == 'telegram':
            result = process_telegram_job(job)
        elif job.platform.name == 'twitter':
            result = process_twitter_job(job)
        elif job.platform.name == 'facebook':
            result = process_facebook_job(job)
        elif job.platform.name == 'instagram':
            result = process_instagram_job(job)
        else:
            raise ValueError(f"Unknown platform: {job.platform.name}")
        
        # Mark success
        job.mark_success(result.get('response', {}), result.get('message_id'))
        
        PostLog.objects.create(
            job=job,
            action='success',
            status='success',
            message='Post published successfully'
        )
        
        return {"status": "success", "job_id": job.id}
        
    except Exception as e:
        error_message = str(e)
        
        # Mark failed
        job.mark_failed(error_message)
        
        PostLog.objects.create(
            job=job,
            action='failed',
            status=job.status,
            message=error_message[:500]
        )
        
        return {"status": "failed", "job_id": job.id, "error": error_message}


def process_telegram_job(job):
    """Process a Telegram post job"""
    
    telegram_channel = job.telegram_channel
    if not telegram_channel:
        raise ValueError("Telegram channel not specified for job")
    
    news = job.news
    
    # Initialize Telegram service
    service = TelegramService(telegram_channel.bot_token)
    
    # Format message
    hashtags = telegram_channel.default_hashtags or "#News #BreakingNews"
    formatter = TelegramPostFormatter()
    message = formatter.format_message(news, hashtags=hashtags)
    
    # Check if we should send with image
    if news.image:
        # Send with image
        response = service.send_photo(
            chat_id=telegram_channel.get_chat_id(),
            photo=news.image,
            caption=message,
            parse_mode=telegram_channel.parse_mode
        )
    else:
        # Send text only
        response = service.send_message(
            chat_id=telegram_channel.get_chat_id(),
            text=message,
            parse_mode=telegram_channel.parse_mode
        )
    
    # Check response
    if not response.get('ok'):
        error = response.get('description', 'Unknown error')
        raise Exception(f"Telegram API error: {error}")
    
    result = response.get('result', {})
    message_id = result.get('message_id')
    
    return {
        'response': response,
        'message_id': str(message_id) if message_id else None
    }


def process_twitter_job(job):
    """Process a Twitter/X post job"""
    # TODO: Implement Twitter API v2
    raise NotImplementedError("Twitter integration not yet implemented")


def process_facebook_job(job):
    """Process a Facebook post job"""
    # TODO: Implement Facebook Graph API
    raise NotImplementedError("Facebook integration not yet implemented")


def process_instagram_job(job):
    """Process an Instagram post job"""
    # TODO: Implement Instagram Graph API
    raise NotImplementedError("Instagram integration not yet implemented")


@shared_task
def process_pending_jobs():
    """Process all pending jobs"""
    
    pending_jobs = PostJob.objects.filter(
        status__in=['pending', 'failed']
    ).filter(
        models.Q(next_retry_at__isnull=True) | models.Q(next_retry_at__lte=timezone.now())
    ).exclude(
        platform__enabled=False
    )[:50]  # Process 50 at a time
    
    processed = 0
    for job in pending_jobs:
        process_post_job.delay(job.id)
        processed += 1
    
    return {"processed": processed}


@shared_task
def cleanup_old_jobs(days=30):
    """Clean up old successful jobs"""
    cutoff = timezone.now() - timezone.timedelta(days=days)
    
    deleted = PostJob.objects.filter(
        status='success',
        created_at__lt=cutoff
    ).delete()
    
    return {"deleted": deleted[0]}