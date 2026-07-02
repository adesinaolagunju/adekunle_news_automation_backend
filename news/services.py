# news/services.py
import requests
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction
from .models import News, NewsFilterRule

class NewsFetcher:
    """Fetch news from Ubuntu Report API"""
    
    API_URL = "https://ubuntureport.onrender.com/api/news/"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'UbuntuNewsBot/1.0'
        })
    
    def fetch_news(self, time_frame='today', max_pages=5):
        """
        Fetch news from the API
        
        Args:
            time_frame: 'today', 'yesterday', 'last_7_days', etc.
            max_pages: Maximum number of pages to fetch
        """
        all_news = []
        url = self.API_URL
        pages_fetched = 0
        
        # Add time_frame filter
        if time_frame:
            if '?' in url:
                url += f'&time_frame={time_frame}'
            else:
                url += f'?time_frame={time_frame}'
        
        while url and pages_fetched < max_pages:
            try:
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                data = response.json()
                
                all_news.extend(data.get('results', []))
                
                url = data.get('next')
                pages_fetched += 1
                
            except requests.exceptions.RequestException as e:
                print(f"Error fetching news: {e}")
                break
        
        return all_news
    
    def process_news(self, news_list):
        """Process fetched news, save new ones"""
        saved_count = 0
        new_news = []
        
        with transaction.atomic():
            for item in news_list:
                api_id = item.get('id')
                
                # Check if news already exists
                if News.objects.filter(api_news_id=api_id).exists():
                    continue
                
                # Create new news
                news = News.create_from_api(item)
                new_news.append(news)
                saved_count += 1
        
        return saved_count, new_news
    
    def should_post_news(self, news):
        """Check if news should be posted based on admin rules"""
        # Get all enabled rules
        rules = NewsFilterRule.objects.filter(enabled=True)
        
        for rule in rules:
            value_lower = rule.value.lower()
            
            if rule.rule_type == 'category':
                match = news.category.lower() == value_lower
            elif rule.rule_type == 'country':
                match = (news.country or '').lower() == value_lower
            elif rule.rule_type == 'source':
                match = news.source.lower() == value_lower
            elif rule.rule_type == 'keyword':
                match = (value_lower in news.title.lower() or 
                        value_lower in (news.summary or '').lower())
            else:
                continue
            
            if rule.rule_action == 'exclude' and match:
                return False
            
            if rule.rule_action == 'include' and not match:
                return False
        
        # If there are include rules, we need at least one match
        include_rules = rules.filter(rule_action='include')
        if include_rules.exists():
            for rule in include_rules:
                if rule.rule_type == 'category' and news.category.lower() == rule.value.lower():
                    return True
                if rule.rule_type == 'country' and (news.country or '').lower() == rule.value.lower():
                    return True
                if rule.rule_type == 'source' and news.source.lower() == rule.value.lower():
                    return True
                if rule.rule_type == 'keyword':
                    if (rule.value.lower() in news.title.lower() or 
                        rule.value.lower() in (news.summary or '').lower()):
                        return True
            
            return False
        
        return True