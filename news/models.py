# news/models.py
from django.db import models
from django.utils import timezone

class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Categories'

class Country(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=2, blank=True, null=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Countries'

class News(models.Model):
    # API data
    api_news_id = models.IntegerField(unique=True, db_index=True)
    title = models.TextField()
    summary = models.TextField(blank=True)
    link = models.URLField(max_length=500)
    image = models.URLField(max_length=500, blank=True, null=True)
    published = models.DateTimeField()
    
    # Metadata
    category = models.CharField(max_length=100, db_index=True)
    country = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    source = models.CharField(max_length=100, db_index=True)
    
    # Local tracking
    fetched_at = models.DateTimeField(auto_now_add=True)
    is_processed = models.BooleanField(default=False, db_index=True)
    
    class Meta:
        ordering = ['-published']
        indexes = [
            models.Index(fields=['category', 'source']),
            models.Index(fields=['published', 'api_news_id']),
        ]
    
    def __str__(self):
        return self.title[:100]
    
    @classmethod
    def create_from_api(cls, api_data):
        """Create a News instance from API data"""
        return cls.objects.create(
            api_news_id=api_data['id'],
            title=api_data['title'],
            summary=api_data.get('summary', ''),
            link=api_data['link'],
            image=api_data.get('image'),
            published=api_data['published'],
            category=api_data.get('category', 'general'),
            country=api_data.get('country'),
            source=api_data.get('source', 'Unknown'),
        )

class NewsFilterRule(models.Model):
    """Admin-defined rules for filtering news"""
    
    RULE_TYPES = [
        ('category', 'Category'),
        ('country', 'Country'),
        ('source', 'Source'),
        ('keyword', 'Keyword'),
    ]
    
    RULE_ACTIONS = [
        ('include', 'Include'),
        ('exclude', 'Exclude'),
    ]
    
    rule_type = models.CharField(max_length=20, choices=RULE_TYPES)
    rule_action = models.CharField(max_length=10, choices=RULE_ACTIONS, default='include')
    value = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.get_rule_action_display()} {self.get_rule_type_display()}: {self.value}"
    
    class Meta:
        ordering = ['rule_type', 'value']