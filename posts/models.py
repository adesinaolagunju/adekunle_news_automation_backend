# posts/models.py
from django.db import models
from django.utils import timezone
from news.models import News
from social.models import BufferAccount, SocialPlatform, TelegramChannel

class PostJob(models.Model):
    """Queue job for posting to platforms"""
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('permanent_fail', 'Permanent Failure'),
        ('skipped', 'Skipped'),
    ]
    
    news = models.ForeignKey(News, on_delete=models.CASCADE, related_name='post_jobs')
    platform = models.ForeignKey(SocialPlatform, on_delete=models.CASCADE)
    
    # If platform is Telegram, link to specific channel
    telegram_channel = models.ForeignKey(TelegramChannel, on_delete=models.CASCADE, 
                                         null=True, blank=True)

    # If platform is Buffer, link to the Buffer account used to publish
    buffer_account = models.ForeignKey(
        BufferAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='post_jobs'
    )
    
    # Job status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', 
                              db_index=True)
    
    # Result data
    posted_at = models.DateTimeField(null=True, blank=True)
    response = models.JSONField(default=dict, blank=True)
    message_id = models.CharField(max_length=100, blank=True, null=True)
    
    # Retry logic
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, null=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'platform']),
            models.Index(fields=['next_retry_at']),
        ]
    
    def __str__(self):
        return f"{self.news.title[:50]} -> {self.platform.name}"
    
    def should_retry(self):
        """Check if the job should be retried"""
        if self.status not in ['failed', 'permanent_fail']:
            return False
        
        if self.retry_count >= self.max_retries:
            return False
        
        if self.next_retry_at and self.next_retry_at > timezone.now():
            return False
        
        return True
    
    def mark_success(self, response_data, message_id=None):
        """Mark the job as successful"""
        self.status = 'success'
        self.posted_at = timezone.now()
        self.response = response_data
        self.message_id = message_id
        self.save()
        
        # Mark news as processed if all jobs for this news are done
        self._update_news_processed()
    
    def mark_failed(self, error_message):
        """Mark the job as failed and schedule retry if possible"""
        self.last_error = error_message
        self.retry_count += 1
        
        if self.retry_count >= self.max_retries:
            self.status = 'permanent_fail'
        else:
            self.status = 'failed'
            # Exponential backoff: 5, 30, 120 minutes
            delay_minutes = [5, 30, 120][self.retry_count - 1] if self.retry_count <= 3 else 120
            self.next_retry_at = timezone.now() + timezone.timedelta(minutes=delay_minutes)
        
        self.save()
    
    def _update_news_processed(self):
        """Update news.is_processed if all jobs are complete"""
        if self.news.post_jobs.filter(status__in=['pending', 'processing']).exists():
            return
        
        self.news.is_processed = True
        self.news.save()

class PostLog(models.Model):
    """Audit log for all posts"""
    
    job = models.ForeignKey(PostJob, on_delete=models.CASCADE, related_name='logs')
    action = models.CharField(max_length=50)
    status = models.CharField(max_length=20)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
