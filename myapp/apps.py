from django.apps import AppConfig


class MyappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "myapp"

    def ready(self):
        # Register auto-assign signals for default payment methods
        from myapp.Utils.payment_method_signals import register
        register()
