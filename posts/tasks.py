# posts/tasks.py
import re
import urllib.parse

import time

from celery import shared_task
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.db import transaction
from .models import PostJob, PostLog
from social.buffer import BufferService, BufferRateLimitError
from social.models import BufferAccount

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
        if job.platform.name == 'buffer':
            result = process_buffer_job(job)
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

    except BufferRateLimitError as e:
        error_message = str(e)
        job.mark_failed(error_message)
        if e.retry_after:
            job.next_retry_at = timezone.now() + timezone.timedelta(seconds=e.retry_after)
            job.save()

        PostLog.objects.create(
            job=job,
            action='rate_limited',
            status=job.status,
            message=f"{error_message[:200]}; retry_after={e.retry_after}"
        )

        return {
            "status": "rate_limited",
            "job_id": job.id,
            "error": error_message,
            "retry_after": e.retry_after,
        }

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


def process_buffer_job(job):
    """Process a Buffer post job via the new GraphQL API.

    Posts to *all* channels in the account's first organization.
    Partial success is tolerated — if some channels succeed and others
    fail, the job is marked successful and the failures are recorded
    in the response.  Only when *every* channel fails does the job
    itself fail.
    """

    buffer_account = job.buffer_account
    if not buffer_account:
        buffer_account = BufferAccount.objects.filter(
            connection_status='connected'
        ).order_by('-updated_at').first()

    if not buffer_account:
        raise ValueError("Buffer account not specified for job")

    news = job.news
    service = BufferService(buffer_account.api_key)

    organizations = service.get_organizations()
    if not organizations:
        raise ValueError("No Buffer organizations found for the connected account")

    organization_id = organizations[0]['id']
    channels = service.get_channels(organization_id)
    if not channels:
        raise ValueError("No Buffer channels available for the connected account")

    def _proxy_image_url(url):
        """Wrap an image URL through a public proxy so Buffer can reach it."""
        if not url:
            return None
        return f"https://images.weserv.nl/?url={urllib.parse.quote(url, safe='')}"

    message = _build_buffer_message(news)
    image_url = _resolve_news_image(news)

    succeeded = []
    failures = []

    for channel in channels:
        channel_id = channel['id']
        service_name = channel.get('service', 'unknown')
        display_name = channel.get('displayName') or channel.get('name', channel_id)

        if succeeded or failures:
            time.sleep(1)

        def _try_post(url):
            return service.publish_now(
                channel_id=channel_id,
                text=message,
                image_url=url,
                service=service_name,
            )

        try:
            post_result = _try_post(image_url)
            succeeded.append({
                'channel_id': channel_id,
                'service': service_name,
                'display_name': display_name,
                'result': post_result,
            })
        except Exception as exc:
            error_str = str(exc)
            if image_url and ("Image URL" in error_str or "accessible" in error_str.lower() or "image dimension" in error_str.lower()):
                # 1st retry: proxy the image URL through images.weserv.nl
                proxied = _proxy_image_url(image_url)
                try:
                    post_result = _try_post(proxied)
                    succeeded.append({
                        'channel_id': channel_id,
                        'service': service_name,
                        'display_name': display_name,
                        'result': post_result,
                        'image_proxied': True,
                    })
                    continue
                except Exception:
                    pass
                # 2nd retry: use the default fallback image
                default_url = settings.DEFAULT_NEWS_IMAGE_URL
                if default_url and default_url != image_url:
                    try:
                        post_result = _try_post(default_url)
                        succeeded.append({
                            'channel_id': channel_id,
                            'service': service_name,
                            'display_name': display_name,
                            'result': post_result,
                            'image_default': True,
                        })
                        continue
                    except Exception:
                        pass
                # 3rd retry: skip image entirely
                try:
                    post_result = _try_post(None)
                    succeeded.append({
                        'channel_id': channel_id,
                        'service': service_name,
                        'display_name': display_name,
                        'result': post_result,
                        'image_skipped': True,
                    })
                    continue
                except Exception:
                    pass
            failures.append({
                'channel_id': channel_id,
                'service': service_name,
                'display_name': display_name,
                'error': str(exc),
            })

    if failures and not succeeded:
        all_errors = "; ".join(
            f"{f['display_name']} ({f['service']}): {f['error']}"
            for f in failures
        )
        raise ValueError(
            f"All {len(failures)} channel(s) failed: {all_errors}"
        )

    return {
        'response': {
            'channels_posted': len(succeeded),
            'channels_attempted': len(channels),
            'succeeded': succeeded,
            'failures': failures,
        },
        'message_id': (
            _extract_buffer_post_id(succeeded[0]['result'])
            if succeeded else None
        ),
    }


def _resolve_news_image(news):
    raw = (news.image or '').strip()
    if raw.startswith(('http://', 'https://')):
        return raw
    return settings.DEFAULT_NEWS_IMAGE_URL


def _build_hashtags(news):
    """Generate a hashtag string from a News item's category, country, and source.

    Always includes ``#BreakingNews`` and ``#Latest``, then the category
    (if present), then the country (if present), then the source (if present).
    Non-alphanumeric characters are stripped so every tag is a valid hashtag.
    """
    tags = ["#BreakingNews", "#Latest"]

    if news.category:
        clean = re.sub(r"[^a-zA-Z0-9]", "", news.category)
        if clean:
            tags.append(f"#{clean}")

    if news.country:
        clean = re.sub(r"[^a-zA-Z0-9]", "", news.country)
        if clean:
            tags.append(f"#{clean}")

    if news.source:
        clean = re.sub(r"[^a-zA-Z0-9]", "", news.source)
        if clean:
            tags.append(f"#{clean}")

    return " ".join(tags)


def _build_buffer_message(news):
    """Create a plain-text Buffer message."""
    summary = (news.summary or '').strip()
    if summary:
        summary = summary[:500]

    hashtags = _build_hashtags(news)

    parts = [f"📰 {news.title}"]
    if summary:
        parts.extend(["", summary])
    parts.extend([
        "",
        "Read More👇",
        news.link,
        "",
        "",
        hashtags,
    ])
    return "\n".join(parts)


def _extract_buffer_post_id(response):
    """Extract a post id from a single queue_post/createPost response.

    The new GraphQL API returns ``{id: "...", text: "...", dueAt: "..."}``
    from the ``CreatePost`` mutation.
    """
    if isinstance(response, dict):
        return str(response.get('id')) if response.get('id') else None
    return None


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
