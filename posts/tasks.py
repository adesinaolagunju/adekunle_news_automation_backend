# posts/tasks.py
import re
import urllib.parse
import contextlib

import time

from django.conf import settings
from django.db import close_old_connections, models, connection
from django.utils import timezone
from django.db import transaction
from .models import PostJob, PostLog, ConcurrentTaskLock
from social.buffer import BufferService, BufferRateLimitError
from social.models import BufferAccount


class AlreadyRunning(Exception):
    """Raised when a distributed task lock cannot be acquired."""


@contextlib.contextmanager
def advisory_lock(lock_id):
    """Acquire a PostgreSQL advisory lock on a dedicated connection.

    Unlike ``SELECT … FOR UPDATE`` the advisory lock is held on a
    **raw** connection that Django's ``close_old_connections()`` will
    not touch, so the lock survives Task-3 style connection releases.

    Parameters
    ----------
    lock_id : int
        A unique numeric identifier for the lock (use the PostgreSQL
        hash of the task name for clarity).

    Raises
    ------
    AlreadyRunning
        If another session already holds the advisory lock.
    """
    raw = connection.cursor().connection  #底层 psycopg2 connection
    try:
        cur = raw.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", [lock_id])
        acquired = cur.fetchone()[0]
        cur.close()
        if not acquired:
            raise AlreadyRunning(
                f"Advisory lock {lock_id} is held by another session"
            )
        yield
    finally:
        cur = raw.cursor()
        cur.execute("SELECT pg_advisory_unlock(%s)", [lock_id])
        cur.close()


def _lock_id_for(task_name):
    """Deterministic integer from a task name for ``pg_advisory_lock``."""
    return abs(hash(task_name)) % (2**31 - 1)


@contextlib.contextmanager
def task_lock(task_name, stale_after=None):
    """Context manager that prevents concurrent executions of *task_name*.

    Uses PostgreSQL ``SELECT … FOR UPDATE`` on a dedicated row to
    atomically check-and-set a ``locked_at`` timestamp.  The row lock
    is held only during the short check transaction — the actual work
    runs outside any transaction so long HTTP calls do not hold
    database resources.

    Parameters
    ----------
    task_name : str
        Unique name for the operation (e.g. ``'fetch_recent'``).
    stale_after : int, optional
        Seconds after which a ``locked_at`` timestamp is considered
        stale (default ``ConcurrentTaskLock.stale_after``).

    Raises
    ------
    AlreadyRunning
        If another instance is currently executing the same task and
        the lock has not gone stale.
    """
    if stale_after is None:
        stale_after = ConcurrentTaskLock.stale_after

    with transaction.atomic():
        lock, _ = ConcurrentTaskLock.objects.select_for_update().get_or_create(
            task_name=task_name,
        )
        if (
            lock.locked_at
            and (timezone.now() - lock.locked_at).total_seconds() < stale_after
        ):
            raise AlreadyRunning(
                f"{task_name} is already running "
                f"(started at {lock.locked_at.isoformat()})"
            )
        lock.locked_at = timezone.now()
        lock.save(update_fields=["locked_at"])

    # Transaction committed — row lock released, timestamp persists.
    try:
        yield
    finally:
        ConcurrentTaskLock.objects.filter(task_name=task_name).update(
            locked_at=None
        )

def process_post_job(job_id):
    """Process a single post job.

    Designed to be safe when called from a ThreadPoolExecutor thread:
    closes stale connections before and after ORM access so that
    thread-local database sessions are released promptly.

    Connection lifecycle:
      1. Open connection for DB reads (load job, create start log)
      2. Release connection before HTTP calls (Buffer API can take minutes)
      3. Reopen connection only for final DB writes (mark status, create logs)
      4. Release connection in finally block
    """
    close_old_connections()
    try:
        try:
            job = PostJob.objects.select_related('news', 'platform', 'buffer_account').get(id=job_id)
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

        # Release DB connection before potentially long HTTP calls.
        # process_buffer_job() makes 3-14 HTTP requests to Buffer API
        # (30s timeout each) — no DB access needed during that time.
        close_old_connections()

        try:
            if job.platform.name == 'buffer':
                result = process_buffer_job(job)
            else:
                raise ValueError(f"Unknown platform: {job.platform.name}")

            # HTTP calls done — reopen connection for DB writes.
            # (close_old_connections is a no-op here if process_buffer_job
            # made no DB calls, but keeps the lifecycle explicit and safe
            # for the fallback query path in process_buffer_job.)
            close_old_connections()

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
            close_old_connections()

            error_message = str(e)
            job.mark_failed(error_message)
            if e.retry_after:
                job.next_retry_at = timezone.now() + timezone.timedelta(seconds=e.retry_after)
                job.save(update_fields=["next_retry_at"])

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
            close_old_connections()

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
    finally:
        close_old_connections()


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
