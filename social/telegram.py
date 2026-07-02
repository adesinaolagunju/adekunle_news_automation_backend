# social/telegram.py
import requests
import json
from datetime import datetime
from django.conf import settings

class TelegramService:
    """Service for interacting with Telegram Bot API"""
    
    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
    
    def _request(self, method, params=None, data=None, files=None):
        """Make API request to Telegram"""
        url = f"{self.base_url}/{method}"
        
        try:
            response = requests.post(url, params=params, data=data, files=files)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            return {'ok': False, 'description': str(e)}
    
    def test_connection(self):
        """Test if the bot token is valid"""
        response = self._request('getMe')
        if response.get('ok'):
            return True, response.get('result', {})
        return False, response.get('description', 'Invalid token')
    
    def get_channel_info(self, chat_id):
        """Get channel details"""
        response = self._request('getChat', params={'chat_id': chat_id})
        if response.get('ok'):
            return True, response.get('result', {})
        return False, response.get('description', 'Failed to get channel info')
    
    def get_chat_id_from_username(self, username):
        """Get chat ID from username"""
        # First, try to get updates to find the chat
        response = self._request('getUpdates', params={'limit': 1})
        if response.get('ok'):
            updates = response.get('result', [])
            if updates and 'message' in updates[0]:
                chat = updates[0]['message'].get('chat', {})
                if chat.get('username') == username.lstrip('@'):
                    return True, chat.get('id')
        
        # If not found, the bot might need to be added first
        return False, f"Channel @{username} not found. Make sure the bot is added as admin."
    
    def send_message(self, chat_id, text, parse_mode='HTML', 
                     disable_preview=False, reply_markup=None):
        """Send a text message to a channel"""
        data = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': disable_preview,
        }
        
        if reply_markup:
            data['reply_markup'] = json.dumps(reply_markup)
        
        response = self._request('sendMessage', data=data)
        return response
    
    def send_photo(self, chat_id, photo, caption=None, parse_mode='HTML', reply_markup=None):
        """Send a photo to a channel"""
        data = {
            'chat_id': chat_id,
            'photo': photo,
            'parse_mode': parse_mode,
        }
        
        if caption:
            data['caption'] = caption
        
        if reply_markup:
            data['reply_markup'] = json.dumps(reply_markup)
        
        response = self._request('sendPhoto', data=data)
        return response
    
    def send_media_group(self, chat_id, media, caption=None, parse_mode='HTML'):
        """Send multiple photos as an album"""
        data = {
            'chat_id': chat_id,
            'media': json.dumps(media),
        }
        
        if caption:
            data['caption'] = caption
            data['parse_mode'] = parse_mode
        
        response = self._request('sendMediaGroup', data=data)
        return response
    
    def create_inline_button(self, text, url):
        """Create an inline keyboard button"""
        return {
            'inline_keyboard': [[{'text': text, 'url': url}]]
        }


class TelegramPostFormatter:
    """Format news for Telegram posts"""
    
    @staticmethod
    def format_message(news, template=None, hashtags=None):
        """Format news into a Telegram message"""
        
        # Default template
        if template is None:
            template = """
<b>📰 {title}</b>

{summary}

🔗 <a href="{link}">Read Full Story</a>

{hashtags}
            """
        
        # Prepare data
        data = {
            'title': news.title,
            'summary': news.summary[:500] if news.summary else '',
            'link': news.link,
            'hashtags': hashtags or '#News'
        }
        
        # Format template
        message = template.format(**data)
        return message.strip()
    
    @staticmethod
    def create_short_message(news, hashtags=None):
        """Create a shorter version of the message"""
        template = """
<b>{title}</b>

{summary}...

<a href="{link}">📖 Continue Reading</a>

{hashtags}
        """
        data = {
            'title': news.title[:100],
            'summary': news.summary[:200] if news.summary else '',
            'link': news.link,
            'hashtags': hashtags or '#News'
        }
        return template.format(**data).strip()
    
    @staticmethod
    def create_breaking_news(news, hashtags=None):
        """Create a breaking news format"""
        template = """
🚨 <b>BREAKING NEWS</b>

<b>{title}</b>

{summary}

📌 <a href="{link}">Read Full Story</a>

{hashtags}
        """
        data = {
            'title': news.title,
            'summary': news.summary[:300] if news.summary else '',
            'link': news.link,
            'hashtags': hashtags or '#BreakingNews'
        }
        return template.format(**data).strip()