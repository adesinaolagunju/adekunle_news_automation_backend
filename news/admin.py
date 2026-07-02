# news/admin.py
from django.contrib import admin
from .models import News, Category, Country, NewsFilterRule

@admin.register(News)
class NewsAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'source', 'published', 'is_processed']
    list_filter = ['category', 'source', 'country', 'is_processed']
    search_fields = ['title', 'summary']
    readonly_fields = ['api_news_id', 'fetched_at']
    ordering = ['-published']

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'enabled']
    list_editable = ['enabled']

@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'enabled']
    list_editable = ['enabled']

@admin.register(NewsFilterRule)
class NewsFilterRuleAdmin(admin.ModelAdmin):
    list_display = ['rule_type', 'rule_action', 'value', 'enabled']
    list_editable = ['enabled']
    list_filter = ['rule_type', 'rule_action']

