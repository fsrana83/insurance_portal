from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from .audit import connect_audit

        connect_audit()

        from django.db.backends.signals import connection_created

        def wal(sender, connection, **kw):
            if connection.vendor == "sqlite":
                try:
                    connection.cursor().execute("PRAGMA journal_mode=WAL;")
                except Exception:
                    pass

        connection_created.connect(wal, weak=False)
