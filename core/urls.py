
# core/urls.py
from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView
from drf_spectacular.views import SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # API endpoints
    path('api/', include('api.urls')),
    
    # Swagger documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    
    # Use custom template for Swagger UI
    path('api/docs/', SpectacularSwaggerView.as_view(
        template_name='swagger-ui.html',  # <-- Your custom template
        url_name='schema'
    ), name='swagger-ui'),

    # path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui')
    
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]