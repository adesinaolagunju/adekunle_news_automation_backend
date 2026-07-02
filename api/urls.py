# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import *

# Create router
router = DefaultRouter()
router.register(r'news', NewsViewSet, basename='news')
router.register(r'categories', CategoryViewSet, basename='categories')
router.register(r'countries', CountryViewSet, basename='countries')
router.register(r'filter-rules', FilterRuleViewSet, basename='filter-rules')
router.register(r'telegram-channels', TelegramChannelViewSet, basename='telegram-channels')
router.register(r'platforms', SocialPlatformViewSet, basename='platforms')
router.register(r'posts', PostJobViewSet, basename='posts')

urlpatterns = [
    # Auth endpoints
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/user/', UserView.as_view(), name='user'),
    
    # Stats endpoints
    path('stats/dashboard/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('stats/platforms/', PlatformStatsView.as_view(), name='platform-stats'),
    
    # Settings endpoints
    path('settings/', SystemSettingsView.as_view(), name='settings'),
    
    # Include router URLs
    path('', include(router.urls)),
]