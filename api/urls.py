# api/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import *

# Create router
router = DefaultRouter()
router.register(r'news', NewsViewSet, basename='news')
router.register(r'categories', CategoryViewSet, basename='categories')
router.register(r'countries', CountryViewSet, basename='countries')
router.register(r'filter-rules', FilterRuleViewSet, basename='filter-rules')
router.register(r'telegram-channels', TelegramChannelViewSet, basename='telegram-channels')
router.register(r'platforms', SocialPlatformViewSet, basename='platforms')  # <-- Social platform CRUD
router.register(r'posts', PostJobViewSet, basename='posts')

urlpatterns = [
    # Auth endpoints
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/logout/', LogoutView.as_view(), name='logout'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('auth/user/', UserView.as_view(), name='user'),
    
    # Stats endpoints
    path('stats/dashboard/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('stats/platforms/', PlatformStatsView.as_view(), name='platform-stats'),
    
    # Settings endpoints
    path('settings/', SystemSettingsView.as_view(), name='settings'),

    # Buffer endpoints
    path('buffer/connect/', BufferConnectView.as_view(), name='buffer-connect'),
    path('buffer/status/', BufferStatusView.as_view(), name='buffer-status'),
    path('buffer/profiles/', BufferProfilesView.as_view(), name='buffer-profiles'),
    path('buffer/disconnect/', BufferDisconnectView.as_view(), name='buffer-disconnect'),
    path('buffer/test/', BufferTestView.as_view(), name='buffer-test'),
    
    # Include router URLs
    path('', include(router.urls)),
]
